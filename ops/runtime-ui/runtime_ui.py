"""Compose the retained Workbench UI with the authoritative TLS REST API.

This service never opens the database or launches workers. Default mode uses
caller-supplied API credentials; explicit private MVP mode uses one protected
server-side token for one project. The fixed upstream uses installation trust and
an explicit SNI hostname while its TCP connection stays on loopback.
"""
from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
from html import escape
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import stat

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.middleware.trustedhost import TrustedHostMiddleware

UI_REVISION = "9e72026dce285c99085f0180d90b13144641b2c2"
MAX_REQUEST = 1024 * 1024
MAX_RESPONSE = 8 * 1024 * 1024
HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'self'; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'",
}
REQUEST_HEADERS = ("authorization", "content-type", "idempotency-key", "if-match", "accept")
RESPONSE_HEADERS = ("content-type", "etag", "retry-after", "www-authenticate")
TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\Z")
WORKFLOW_EVENTS = {"hold", "release_hold", "defer", "resume_deferred", "reopen", "update_control"}


def _read_workbench_token(path: Path) -> str:
    """Read an explicitly selected, protected server-side bearer credential."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or
                    not (info.st_mode & stat.S_IRUSR) or info.st_mode & 0o077):
                raise ValueError("Workbench token file is not a protected regular file")
            if info.st_size < 32 or info.st_size > 4097:
                raise ValueError("Workbench token file is unavailable or invalid")
            token = os.read(descriptor, 4098).decode("utf-8").removesuffix("\n")
        finally:
            os.close(descriptor)
    except (OSError, UnicodeError) as error:
        raise ValueError("Workbench token file is unavailable or invalid") from None
    core = token.rstrip("=")
    if (not 32 <= len(token) <= 4096 or len(token) - len(core) > 4 or
            not core or any(not char.isascii() or not (char.isalnum() or char in "-._~+/") for char in core)):
        raise ValueError("Workbench token file is unavailable or invalid")
    return token


def _private_route(request: Request, project: str, body: bytes = b"") -> bool:
    """Allow only the Workbench task, history, workflow and board API surface."""
    raw = request.scope.get("raw_path", b"")
    path = request.url.path
    if not isinstance(raw, bytes) or raw != path.encode("ascii", "ignore") or any(
            marker in raw for marker in (b"%", b"\\", b"//", b"\x00")):
        return False
    parts = path.split("/")
    if len(parts) < 6 or parts[:4] != ["", "api", "v1", "projects"] or parts[4] != project:
        return False
    method = request.method
    resource = parts[5:]
    query = request.scope.get("query_string", b"")
    items = request.query_params.multi_items()
    if (not isinstance(query, bytes) or len(query) > 2048 or len(items) > 4 or
            len({key for key, _ in items}) != len(items)):
        return False
    if method == "GET" and resource == ["workflow-board"]:
        allowed_query = {"limit", "offset"}
    elif method == "GET" and resource == ["tasks"]:
        allowed_query = {"limit", "offset", "by_id", "after_task_id"}
    elif method == "GET" and len(resource) == 3 and resource[2] == "history":
        allowed_query = {"limit", "offset"}
    else:
        allowed_query = set()
    if set(request.query_params.keys()) - allowed_query:
        return False
    for key, value in items:
        if key in {"limit", "offset"}:
            if not value.isdigit() or len(value) > 7:
                return False
            number = int(value)
            if (key == "limit" and not 1 <= number <= 100) or (key == "offset" and number > 1_000_000):
                return False
        if key == "by_id" and value != "true":
            return False
        if key == "after_task_id" and not TASK_ID.fullmatch(value):
            return False
    if method == "GET" and resource == ["tasks"]:
        return True
    if method == "GET" and resource == ["workflow-board"]:
        return True
    if len(resource) >= 2 and resource[0] == "tasks" and TASK_ID.fullmatch(resource[1]):
        if method == "GET" and len(resource) == 2:
            return True
        if method == "GET" and len(resource) == 3 and resource[2] in {"history", "workflow", "lineage"}:
            return True
        if method == "PATCH" and len(resource) == 2:
            return True
        if method == "POST" and len(resource) == 4 and resource[2] == "actions":
            return resource[3] in {"rework", "reassess", "defer", "resume", "ready"}
        if method == "POST" and len(resource) == 3 and resource[2] == "workflow":
            try:
                payload = json.loads(body)
            except (ValueError, UnicodeDecodeError, RecursionError):
                return False
            event = payload.get("event") if isinstance(payload, dict) else None
            return isinstance(event, str) and event in WORKFLOW_EVENTS
    if method == "POST" and resource == ["tasks"]:
        return True
    return False


def _same_origin_mutation(request: Request) -> bool:
    origin = request.headers.get("origin")
    expected = f"{request.url.scheme}://{request.url.netloc}"
    fetch_site = request.headers.get("sec-fetch-site")
    return (origin == expected and request.headers.get("x-skybuild-workbench") == "1" and
            fetch_site in {None, "same-origin"})


def _replace_once(source: str, before: str, after: str, label: str) -> str:
    if source.count(before) != 1:
        raise ValueError(f"Retained {label} changed; private-mode transformation needs review")
    return source.replace(before, after, 1)


def _private_tasks_script(source: str) -> str:
    source = _replace_once(source,
        '  const previewMode = document.documentElement.dataset.skybuildPreview === "true";',
        '  const previewMode = document.documentElement.dataset.skybuildPreview === "true";\n'
        '  const privateConfig = window.SKYBUILD_WORKBENCH_PRIVATE || {enabled: false};\n'
        '  const privateMode = privateConfig.enabled === true;', "task page script")
    source = _replace_once(source,
        '  let token = previewMode ? "local-preview" : "", project = previewMode ? "skybuild" : "", tasks = [], selectedTask = null, selectedHistory = [], cursor = null;',
        '  let token = privateMode ? "server-managed" : (previewMode ? "local-preview" : ""), project = privateMode ? privateConfig.project : (previewMode ? "skybuild" : ""), tasks = [], selectedTask = null, selectedHistory = [], cursor = null;',
        "task page connection state")
    source = _replace_once(source, '    const headers = {Authorization: `Bearer ${token}`};',
        '    const headers = privateMode ? {"X-Skybuild-Workbench": "1"} : {Authorization: `Bearer ${token}`};',
        "task page request headers")
    source = _replace_once(source,
        '        notify("Authentication failed. Connect with a valid token.", true);',
        '        notify(privateMode ? "Configured Workbench credential was rejected. Contact the installation operator." : "Authentication failed. Connect with a valid token.", true);',
        "task page authentication message")
    source = _replace_once(source,
        '  renderTasks();\n  renderTaskSummary(null);\n  renderHistory();\n  controls();\n})();',
        '  if (privateMode) {\n'
        '    formConnection.hidden = true;\n'
        '    byId("project").closest("div").hidden = true;\n'
        '    byId("connection-title").hidden = true;\n'
        '    byId("logout").hidden = true;\n'
        '    token = "server-managed"; project = privateConfig.project;\n'
        '    byId("notice").textContent = "Loading SkyBuild tasks…";\n'
        '  }\n'
        '  renderTasks();\n  renderTaskSummary(null);\n  renderHistory();\n  controls();\n'
        '  if (privateMode) void perform(async () => { await loadTasks(); notify(`Loaded ${tasks.length} SkyBuild tasks.`); });\n})();',
        "task page startup")
    return source


def _private_workflow_script(source: str) -> str:
    source = _replace_once(source,
        '  let token = "", project = "", selected = null, busy = false, stale = false, epoch = 0, controller = null, reconcileCursor = null, structuralPlan = null, historyOffset = 0, historyHasMore = false, taskCursor = null, taskHasMore = false;',
        '  const privateConfig = window.SKYBUILD_WORKBENCH_PRIVATE || {enabled: false};\n'
        '  const privateMode = privateConfig.enabled === true;\n'
        '  let token = privateMode ? "server-managed" : "", project = privateMode ? privateConfig.project : "", selected = null, busy = false, stale = false, epoch = 0, controller = null, reconcileCursor = null, structuralPlan = null, historyOffset = 0, historyHasMore = false, taskCursor = null, taskHasMore = false;',
        "workflow page connection state")
    source = _replace_once(source, '    const headers = { Authorization: `Bearer ${token}` };',
        '    const headers = privateMode ? {"X-Skybuild-Workbench": "1"} : { Authorization: `Bearer ${token}` };',
        "workflow page request headers")
    source = _replace_once(source, '    const available = new Set(workflowView.available_actions || []);',
        '    const available = new Set((workflowView.available_actions || []).filter(name => !privateMode || name !== "claim"));',
        "workflow page action list")
    source = _replace_once(source,
        '    if (!controlNames[name] || !(workflowView.available_actions || []).includes(name)) return;',
        '    if (!controlNames[name] || (privateMode && name === "claim") || !(workflowView.available_actions || []).includes(name)) return;',
        "workflow page mutation guard")
    source = _replace_once(source,
        '        disconnect(); notice("Authentication failed (401). Connect with a valid token.", true);',
        '        disconnect(); notice(privateMode ? "Configured Workbench credential was rejected. Contact the installation operator." : "Authentication failed (401). Connect with a valid token.", true);',
        "workflow page authentication message")
    source = _replace_once(source, '  controls();\n})();',
        '  if (privateMode) {\n'
        '    connection.hidden = true; byId("connection-title").hidden = true; byId("logout").hidden = true;\n'
        '    byId("reconcile-due").hidden = true; structure.hidden = true;\n'
        '    byId("notice").textContent = "Loading SkyBuild tasks and workflow…";\n'
        '    void perform(async () => { await loadTasks(); await loadBoard(); notice(`Loaded ${byId("task-count").textContent}.`); });\n'
        '  }\n  controls();\n})();', "workflow page startup")
    return source


def create_app(*, ui_checkout: Path, api_checkout: Path, ca_file: Path,
               backend_hostname: str, backend_port: int = 8000,
               backend_connect_host: str = "127.0.0.1",
               workbench_token_file: Path | None = None,
               workbench_project: str | None = None,
               transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    if not backend_hostname or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-" for c in backend_hostname):
        raise ValueError("Invalid backend TLS hostname")
    if not 1 <= backend_port <= 65535:
        raise ValueError("Invalid backend port")
    if not backend_connect_host or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-" for c in backend_connect_host):
        raise ValueError("Invalid backend connection host")
    if (workbench_token_file is None) != (workbench_project is None):
        raise ValueError("Private Workbench mode requires both token file and project")
    if workbench_project is not None and not TASK_ID.fullmatch(workbench_project):
        raise ValueError("Invalid private Workbench project")
    private_mode = workbench_token_file is not None
    workbench_token = _read_workbench_token(workbench_token_file) if workbench_token_file else None
    ui_static = ui_checkout / "src/skybuild/static"
    api_static = api_checkout / "src/skybuild/static"
    pins = json.loads(Path(__file__).with_name("asset-pins.json").read_text())
    for relative, expected in pins["sha256"].items():
        if relative.startswith("ui/"):
            asset_path = ui_checkout / relative.removeprefix("ui/")
        elif relative.startswith("api-source/"):
            asset_path = api_checkout / relative.removeprefix("api-source/")
        elif relative == "navigation.html":
            asset_path = Path(__file__).with_name(relative)
        else:
            raise ValueError("Unexpected asset pin")
        if hashlib.sha256(asset_path.read_bytes()).hexdigest() != expected:
            raise ValueError("Workbench asset differs from the reviewed snapshot")
    # This captured navigation is static markup, not an import of the old backend.
    nav = Path(__file__).with_name("navigation.html").read_text()
    nav = nav.replace('</nav>', '<a class="workbench-top-link" href="/workbench/workflow">Live Petri workflow</a></nav>')
    tasks_html = (ui_static / "tasks.html").read_text().replace("<!--WORKBENCH_NAV-->", nav)
    tasks_js = (ui_static / "tasks.js").read_text()
    fallback = "tasks = afterTaskId === null && result.length === 0 ? fakeTasks : result;"
    if tasks_js.count(fallback) != 1:
        raise ValueError("Retained UI source changed; review the empty-list behavior")
    tasks_js = tasks_js.replace(fallback, "tasks = result;")
    tasks_html = tasks_html.replace("This is the proposed workflow from the architecture, not a live workflow engine.",
                                    "This diagram interprets journal events. Open Live Petri workflow for the API's authoritative marking and transitions.")
    workflow_html = (api_static / "workbench.html").read_text()
    workflow_html = workflow_html.replace('/workbench/assets/workbench.js', '/workbench/assets/live-workflow.js')
    workflow_html = workflow_html.replace('/workbench/assets/workbench.css', '/workbench/assets/live-workflow.css')
    workflow_html = workflow_html.replace('</head>', '<link rel="stylesheet" href="/workbench/assets/workbench-shell.css"></head>')
    workflow_html = workflow_html.replace('<body>', '<body>' + nav)
    private_bootstrap = ""
    if private_mode:
        private_bootstrap = "window.SKYBUILD_WORKBENCH_PRIVATE = " + json.dumps(
            {"enabled": True, "project": workbench_project}, separators=(",", ":")) + ";\n"
        tasks_html = _replace_once(tasks_html, '<script src="/workbench/assets/tasks.js"',
            '<script src="/workbench/assets/private-mode.js" defer></script>\n  <script src="/workbench/assets/tasks.js"',
            "task page HTML")
        tasks_html = _replace_once(tasks_html, '<div>\n        <h2 id="connection-title"',
            '<div hidden>\n        <h2 id="connection-title"', "task page connection panel")
        tasks_html = _replace_once(tasks_html, '<form id="connection-form"',
            '<form id="connection-form" hidden', "task page connection form")
        tasks_html = _replace_once(tasks_html, 'Enter project and token to load tasks.',
            'Loading SkyBuild tasks…', "task page initial status")
        workflow_html = _replace_once(workflow_html, '<script src="/workbench/assets/live-workflow.js"',
            '<script src="/workbench/assets/private-mode.js" defer></script>\n  <script src="/workbench/assets/live-workflow.js"',
            "workflow page HTML")
        workflow_html = _replace_once(workflow_html, '<h2 id="connection-title">',
            '<h2 id="connection-title" hidden>', "workflow page connection title")
        workflow_html = _replace_once(workflow_html, '<form id="connection-form"',
            '<form id="connection-form" hidden', "workflow page connection form")
        workflow_html = _replace_once(workflow_html, 'Enter a project and token to load tasks.',
            'Loading SkyBuild tasks and workflow…', "workflow page initial status")
        tasks_js = _private_tasks_script(tasks_js)
        workflow_script = _private_workflow_script((api_static / "workbench.js").read_text())
    else:
        workflow_script = (api_static / "workbench.js").read_text()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile=str(ca_file))
    client = httpx.AsyncClient(verify=context, trust_env=False, follow_redirects=False,
                              timeout=httpx.Timeout(30, connect=5), transport=transport,
                              limits=httpx.Limits(max_connections=32, max_keepalive_connections=8))

    @asynccontextmanager
    async def lifespan(app):
        yield
        await client.aclose()

    app = FastAPI(title="SkyBuild running stack", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware,
                       allowed_hosts=["localhost", "127.0.0.1", "[::1]", backend_hostname])

    @app.get("/workbench")
    @app.get("/workbench/tasks")
    async def tasks_page():
        return HTMLResponse(tasks_html, headers=HEADERS)

    @app.get("/workbench/workflow")
    async def workflow_page():
        return HTMLResponse(workflow_html, headers=HEADERS)

    assets = {
        "tasks.js": (tasks_js.encode(), "text/javascript"),
        "tasks.css": ((ui_static / "tasks.css").read_bytes(), "text/css"),
        "workbench-shell.css": ((ui_static / "workbench-shell.css").read_bytes(), "text/css"),
        "workbench.css": ((ui_static / "workbench.css").read_bytes(), "text/css"),
        "live-workflow.js": ((api_static / "workbench.js").read_bytes(), "text/javascript"),
        "live-workflow.css": ((api_static / "workbench.css").read_bytes(), "text/css"),
    }
    if private_mode:
        assets["private-mode.js"] = (private_bootstrap.encode(), "text/javascript")
        assets["live-workflow.js"] = (workflow_script.encode(), "text/javascript")

    @app.get("/workbench/assets/{name}")
    async def asset(name: str):
        if name not in assets:
            return JSONResponse({"detail": "Asset unavailable"}, status_code=404, headers=HEADERS)
        data, media = assets[name]
        return Response(data, media_type=media, headers=HEADERS)

    @app.get("/workbench/runtime")
    async def runtime():
        return JSONResponse({"ui_revision": UI_REVISION,
                             "task_authority": "SkyBuild REST API",
                             "backend_transport": "loopback TLS with installation CA and hostname verification",
                             "fake_task_mode": False, "private_mode": private_mode,
                             "project": workbench_project if private_mode else None}, headers=HEADERS)

    async def forward(request: Request):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            expected_origin = f"{request.url.scheme}://{request.url.netloc}"
            if origin is not None and origin != expected_origin:
                return JSONResponse({"detail": "Same-origin request required"}, status_code=403, headers=HEADERS)
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > MAX_REQUEST:
                return JSONResponse({"detail": "Request too large"}, status_code=413, headers=HEADERS)
        private_task_request = False
        if private_mode:
            if request.headers.get("authorization") is not None:
                return JSONResponse({"detail": "Caller credentials are not accepted in private Workbench mode"},
                                    status_code=403, headers=HEADERS)
            if request.url.path.startswith("/api/v1/"):
                if not _private_route(request, workbench_project, bytes(data)):
                    return JSONResponse({"detail": "Route unavailable in private Workbench mode"},
                                        status_code=404, headers=HEADERS)
                if (request.headers.get("x-skybuild-workbench") != "1" or
                        request.headers.get("sec-fetch-site") not in {None, "same-origin"}):
                    return JSONResponse({"detail": "Same-origin Workbench intent required"},
                                        status_code=403, headers=HEADERS)
                if request.method not in {"GET", "HEAD"} and not _same_origin_mutation(request):
                    return JSONResponse({"detail": "Same-origin Workbench intent required"},
                                        status_code=403, headers=HEADERS)
                private_task_request = True
            elif request.url.path not in {"/health/live", "/health/ready"}:
                return JSONResponse({"detail": "Route unavailable in private Workbench mode"},
                                    status_code=404, headers=HEADERS)
        # The URL authority is immutable. Raw path/query preserve API encodings.
        target = httpx.URL(f"https://{backend_connect_host}:{backend_port}").copy_with(raw_path=request.scope["raw_path"] +
                (b"?" + request.scope["query_string"] if request.scope["query_string"] else b""))
        headers = {key: request.headers[key] for key in REQUEST_HEADERS if key in request.headers}
        headers["host"] = f"{backend_hostname}:{backend_port}"
        if private_task_request:
            headers["authorization"] = f"Bearer {workbench_token}"
        try:
            async with client.stream(request.method, target, headers=headers, content=bytes(data),
                                     extensions={"sni_hostname": backend_hostname}) as upstream:
                response = bytearray()
                async for chunk in upstream.aiter_bytes():
                    response.extend(chunk)
                    if len(response) > MAX_RESPONSE:
                        return JSONResponse({"detail": "API response too large"}, status_code=502, headers=HEADERS)
                response_headers = {key: upstream.headers[key] for key in RESPONSE_HEADERS if key in upstream.headers}
                if private_task_request:
                    secret = workbench_token.encode("utf-8")
                    response = response.replace(secret, b"[redacted]")
                    response_headers = {key: value.replace(workbench_token, "[redacted]")
                                        for key, value in response_headers.items()}
                response_headers.update(HEADERS)
                return Response(bytes(response), status_code=upstream.status_code, headers=response_headers)
        except httpx.HTTPError:
            # Driver errors may contain credential-bearing URLs. Never echo them.
            return JSONResponse({"detail": "SkyBuild API unavailable"}, status_code=502, headers=HEADERS)

    app.add_api_route("/api/v1/{path:path}", forward,
                      methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    app.add_api_route("/health/{path:path}", forward, methods=["GET", "HEAD"])

    @app.api_route("/workbench/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def unavailable(path: str, request: Request):
        if path.startswith("api/") or path.startswith("dev/") or request.method != "GET":
            return JSONResponse({"detail": "Collector or control is not connected to this stack"},
                                status_code=503, headers=HEADERS)
        return HTMLResponse('<!doctype html><html><head><title>SkyBuild unavailable panel</title>'
                            '<link rel="stylesheet" href="/workbench/assets/workbench-shell.css"></head>'
                            '<body>' + nav + '<main><h1>' + escape(path) + '</h1>'
                            '<p>This panel is unavailable. Its live collector is not connected.</p>'
                            '<p><a href="/workbench/tasks">Open live tasks</a></p></main></body></html>',
                            status_code=503, headers=HEADERS)
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ui-checkout", type=Path, required=True)
    parser.add_argument("--api-checkout", type=Path, required=True)
    parser.add_argument("--ca-file", type=Path, required=True)
    parser.add_argument("--backend-hostname", required=True)
    parser.add_argument("--backend-port", type=int, default=8000)
    parser.add_argument("--backend-connect-host", default="127.0.0.1")
    parser.add_argument("--workbench-token-file", type=Path,
                        help="Explicit protected server-side token; enables private MVP mode")
    parser.add_argument("--workbench-project",
                        help="One project ID permitted to use the private Workbench token")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--ssl-certfile", type=Path)
    parser.add_argument("--ssl-keyfile", type=Path)
    parser.add_argument("--container-listener", action="store_true",
                        help="Allow container wildcard bind; publish only approved host interfaces")
    args = parser.parse_args()
    if bool(args.ssl_certfile) != bool(args.ssl_keyfile):
        parser.error("Supply both TLS certificate and key")
    if args.host not in {"127.0.0.1", "::1"} and not args.ssl_certfile:
        parser.error("Non-loopback listeners require TLS")
    if args.host in {"0.0.0.0", "::"} and not args.container_listener:
        parser.error("Bind an explicit loopback or private interface")
    import uvicorn
    app = create_app(ui_checkout=args.ui_checkout, api_checkout=args.api_checkout,
                     ca_file=args.ca_file, backend_hostname=args.backend_hostname,
                     backend_port=args.backend_port,
                     backend_connect_host=args.backend_connect_host,
                     workbench_token_file=args.workbench_token_file,
                     workbench_project=args.workbench_project)
    uvicorn.run(app, host=args.host, port=args.port, access_log=False,
                proxy_headers=False, server_header=False,
                ssl_certfile=str(args.ssl_certfile) if args.ssl_certfile else None,
                ssl_keyfile=str(args.ssl_keyfile) if args.ssl_keyfile else None)


if __name__ == "__main__":
    main()
