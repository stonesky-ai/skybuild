"""Compose the retained Workbench UI with the authoritative TLS REST API.

This service never opens the database or launches workers. API credentials remain
caller supplied. The fixed upstream uses installation trust and an explicit SNI
hostname while its TCP connection stays on loopback.
"""
from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
from html import escape
import hashlib
import json
from pathlib import Path
import ssl

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


def create_app(*, ui_checkout: Path, api_checkout: Path, ca_file: Path,
               backend_hostname: str, backend_port: int = 8000,
               backend_connect_host: str = "127.0.0.1",
               transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    if not backend_hostname or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-" for c in backend_hostname):
        raise ValueError("Invalid backend TLS hostname")
    if not 1 <= backend_port <= 65535:
        raise ValueError("Invalid backend port")
    if not backend_connect_host or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-" for c in backend_connect_host):
        raise ValueError("Invalid backend connection host")
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
                             "fake_task_mode": False}, headers=HEADERS)

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
        # The URL authority is immutable. Raw path/query preserve API encodings.
        target = httpx.URL(f"https://{backend_connect_host}:{backend_port}").copy_with(raw_path=request.scope["raw_path"] +
                (b"?" + request.scope["query_string"] if request.scope["query_string"] else b""))
        headers = {key: request.headers[key] for key in REQUEST_HEADERS if key in request.headers}
        headers["host"] = f"{backend_hostname}:{backend_port}"
        try:
            async with client.stream(request.method, target, headers=headers, content=bytes(data),
                                     extensions={"sni_hostname": backend_hostname}) as upstream:
                response = bytearray()
                async for chunk in upstream.aiter_bytes():
                    response.extend(chunk)
                    if len(response) > MAX_RESPONSE:
                        return JSONResponse({"detail": "API response too large"}, status_code=502, headers=HEADERS)
                response_headers = {key: upstream.headers[key] for key in RESPONSE_HEADERS if key in upstream.headers}
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
                     backend_connect_host=args.backend_connect_host)
    uvicorn.run(app, host=args.host, port=args.port, access_log=False,
                proxy_headers=False, server_header=False,
                ssl_certfile=str(args.ssl_certfile) if args.ssl_certfile else None,
                ssl_keyfile=str(args.ssl_keyfile) if args.ssl_keyfile else None)


if __name__ == "__main__":
    main()
