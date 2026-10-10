import asyncio
from collections import Counter
import json
import importlib.util
from pathlib import Path
import re
import shutil
import subprocess

import httpx
from fastapi.testclient import TestClient
import pytest

from test_runtime_ui_gateway import API, CA, HOST, ROOT

module_spec = importlib.util.spec_from_file_location("runtime_ui_private_under_test", ROOT / "runtime_ui.py")
runtime_ui = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(runtime_ui)

TOKEN = "private-workbench-token-which-must-never-escape"
PROJECT = "skybuild"
USERNAME = "user1"
PASSWORD = "private-test-password"


def test_workbench_compose_arguments_follow_image_entrypoint():
    dockerfile = (ROOT / "Dockerfile").read_text()
    compose = (ROOT / "compose.yaml").read_text()
    entrypoint_line = next(line for line in dockerfile.splitlines() if line.startswith("ENTRYPOINT "))
    entrypoint = json.loads(entrypoint_line.removeprefix("ENTRYPOINT "))
    command_section = compose.split("    command:\n", 1)[1].split("    ports:\n", 1)[0]
    command = re.findall(r"^      - (.+)$", command_section, flags=re.MULTILINE)
    argv = entrypoint + command

    assert entrypoint == ["python", "/runtime-ui/runtime_ui.py"]
    assert argv[:3] == ["python", "/runtime-ui/runtime_ui.py", "--ui-checkout"]
    assert "python" not in command


def test_workbench_docker_sources_are_included_by_root_build_context_filter():
    dockerignore = set((ROOT.parents[1] / ".dockerignore").read_text().splitlines())
    dockerfile = (ROOT / "Dockerfile").read_text()

    assert {"!ops/", "!ops/runtime-ui/", "!ops/runtime-ui/runtime_ui.py",
            "!ops/runtime-ui/asset-pins.json", "!ops/runtime-ui/navigation.html",
            "!ops/runtime-ui/ui/", "!ops/runtime-ui/ui/**"} <= dockerignore
    assert "COPY --chown=1000:1000 ops/runtime-ui/runtime_ui.py ops/runtime-ui/asset-pins.json ops/runtime-ui/navigation.html /runtime-ui/" in dockerfile
    assert "COPY --chown=1000:1000 ops/runtime-ui/ui/ /runtime-ui/ui/" in dockerfile
    assert "!ops/runtime-ui/api-source/" not in dockerignore
    assert "RUN chmod g+x,o+x /app/src/skybuild" in dockerfile
    assert "&& chmod -R a+rX /app/src/skybuild/static" in dockerfile
    assert "--api-checkout\n      - /app" in (ROOT / "compose.yaml").read_text()


def token_file(path: Path, value: str = TOKEN) -> Path:
    path.write_text(value + "\n")
    path.chmod(0o600)
    return path


def private_app(handler, token_path, project=PROJECT):
    password_path = token_path.with_name(token_path.name + ".password")
    password_path.write_text(PASSWORD + "\n")
    password_path.chmod(0o600)
    return runtime_ui.create_app(
        ui_checkout=ROOT / "ui", api_checkout=API, ca_file=CA,
        backend_hostname=HOST, backend_connect_host="api",
        workbench_token_file=token_path, workbench_project=project,
        workbench_username=USERNAME, workbench_password_file=password_path,
        transport=httpx.MockTransport(handler),
    )


def login(client, username=USERNAME, password=PASSWORD):
    response = client.post("/workbench/session", headers={
        "Origin": "https://localhost:8443", "Sec-Fetch-Site": "same-origin",
        "X-Skybuild-Workbench": "1"}, json={"username": username, "password": password})
    return response


def test_private_login_creates_secure_session_and_logout_revokes_it(tmp_path):
    path = token_file(tmp_path / "token")
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=[])

    with TestClient(private_app(handler, path), base_url="https://localhost:8443") as client:
        assert client.get("/workbench", follow_redirects=False).status_code == 303
        assert client.get("/api/v1/projects/skybuild/tasks", headers={
            "X-Skybuild-Workbench": "1"}).status_code == 401
        rejected_logout = client.delete("/workbench/session", headers={
            "Origin": "https://evil.invalid", "X-Skybuild-Workbench": "1"})
        assert rejected_logout.status_code == 403
        login_page = client.get("/workbench/login")
        assert 'src="/workbench/assets/private-login.js" defer' in login_page.text
        assert 'href="/workbench/assets/private-mode.css"' in login_page.text
        assert 'autocomplete="username"' in login_page.text
        assert 'autocomplete="current-password"' in login_page.text
        assert '<form class="login-form" id="login-form" method="post" action="/workbench/session">' in login_page.text
        assert PASSWORD not in login_page.text
        bad = login(client, password="wrong-password")
        assert bad.status_code == 401 and bad.json() == {"detail": "Username or password is incorrect"}
        assert client.get("/api/v1/projects/skybuild/tasks", headers={
            "X-Skybuild-Workbench": "1"}).status_code == 401
        accepted = login(client)
        assert accepted.status_code == 200
        assert accepted.json() == {"username": USERNAME}
        assert client.get("/api/v1/projects/skybuild/tasks", headers={
            "X-Skybuild-Workbench": "1", "Sec-Fetch-Site": "same-origin"}).status_code == 200
        rejected_logout = client.delete("/workbench/session", headers={
            "Origin": "https://evil.invalid", "X-Skybuild-Workbench": "1"})
        assert rejected_logout.status_code == 403
        assert client.get("/api/v1/projects/skybuild/tasks", headers={
            "X-Skybuild-Workbench": "1", "Sec-Fetch-Site": "same-origin"}).status_code == 200
        logout = client.delete("/workbench/session", headers={
            "Origin": "https://localhost:8443", "Sec-Fetch-Site": "same-origin",
            "X-Skybuild-Workbench": "1"})
        assert logout.status_code == 204
        assert "Max-Age=0" in logout.headers["set-cookie"]
        assert client.get("/workbench", follow_redirects=False).status_code == 303
        assert client.get("/api/v1/projects/skybuild/tasks", headers={
            "X-Skybuild-Workbench": "1"}).status_code == 401
    assert len(seen) == 2
    assert all(request.headers["authorization"] == f"Bearer {TOKEN}" for request in seen)
    assert all("cookie" not in request.headers for request in seen)


def test_private_login_requires_same_origin_and_limits_failures(tmp_path):
    path = token_file(tmp_path / "token")
    with TestClient(private_app(lambda request: httpx.Response(200), path),
                    base_url="https://localhost:8443") as client:
        rejected = client.post("/workbench/session", headers={
            "X-Skybuild-Workbench": "1", "Origin": "https://evil.invalid"},
            json={"username": USERNAME, "password": PASSWORD})
        assert rejected.status_code == 403
        for _ in range(runtime_ui.LOGIN_FAILURE_LIMIT):
            assert login(client, password="wrong-password").status_code == 401
        limited = login(client)
        assert limited.status_code == 429
        assert USERNAME not in limited.text and PASSWORD not in limited.text


def test_private_login_rechecks_failure_limit_after_concurrent_body_reads(tmp_path):
    app = private_app(lambda request: httpx.Response(200), token_file(tmp_path / "token"))
    total = 30
    arrived = 0
    body_barrier = asyncio.Event()
    body = json.dumps({"username": USERNAME, "password": "wrong-review-password"}).encode()

    async def request_with_body_barrier():
        nonlocal arrived
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "POST", "scheme": "https", "path": "/workbench/session",
            "raw_path": b"/workbench/session", "query_string": b"", "root_path": "",
            "headers": [
                (b"host", b"localhost:8443"), (b"origin", b"https://localhost:8443"),
                (b"x-skybuild-workbench", b"1"), (b"sec-fetch-site", b"same-origin"),
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
            "client": ("100.80.1.2", 50000), "server": ("localhost", 8443),
        }
        status = None

        async def receive():
            nonlocal arrived
            arrived += 1
            if arrived == total:
                body_barrier.set()
            await body_barrier.wait()
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]

        await app(scope, receive, send)
        return status

    async def run_requests():
        statuses = await asyncio.wait_for(
            asyncio.gather(*(request_with_body_barrier() for _ in range(total))), timeout=10)
        return Counter(statuses)

    assert asyncio.run(run_requests()) == Counter({429: total - runtime_ui.LOGIN_FAILURE_LIMIT,
                                                    401: runtime_ui.LOGIN_FAILURE_LIMIT})


def test_private_pages_bootstrap_only_project_and_never_credential(tmp_path):
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path.endswith("/workflow-board"):
            return httpx.Response(200, json={"columns": [], "tasks": [], "total": 0,
                                             "ready_dependencies_complete": 0,
                                             "ready_dependencies_blocked": 0,
                                             "unenrolled_count": 0, "next_offset": None})
        if request.url.path.endswith("/tasks"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={})

    path = token_file(tmp_path / "token")
    with TestClient(private_app(handler, path), base_url="https://localhost:8443") as client:
        assert client.get("/workbench/tasks", follow_redirects=False).headers["location"] == "/workbench/login"
        login_response = login(client)
        assert login_response.status_code == 200
        cookie = login_response.headers["set-cookie"]
        assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=strict" in cookie
        assert "Max-Age=28800" in cookie
        task_page = client.get("/workbench/tasks").text
        task_script = client.get("/workbench/assets/tasks.js").text
        workflow_page = client.get("/workbench/workflow").text
        workflow_script = client.get("/workbench/assets/live-workflow.js").text
        private_style = client.get("/workbench/assets/private-mode.css")
        bootstrap = client.get("/workbench/assets/private-mode.js").text
        login_page = client.get("/workbench/login", follow_redirects=False)
        runtime = client.get("/workbench/runtime").json()

    assert 'private-mode.js' in task_page and 'private-mode.js' in workflow_page
    assert login_page.status_code == 303 and login_page.headers["location"] == "/workbench"
    assert 'id="workbench-logout"' in task_page and 'id="workbench-logout"' in workflow_page
    assert "workbench/session" in bootstrap
    assert 'private-mode.css' in task_page and 'private-mode.css' in workflow_page
    assert private_style.status_code == 200 and "[hidden] { display: none !important; }" in private_style.text
    assert '<form id="connection-form" hidden' in task_page
    assert '<form id="connection-form" hidden' in workflow_page
    assert '<div hidden>\n        <h2 id="connection-title"' in task_page
    assert '<h2 id="connection-title" hidden>' in workflow_page
    assert "Loading SkyBuild tasks…" in task_page
    assert "Loading SkyBuild tasks and workflow…" in workflow_page
    assert '"enabled":true,"project":"skybuild"' in bootstrap
    assert 'void perform(async () => { await loadTasks(); notify(`Loaded ${tasks.length} SkyBuild tasks.`); });' in task_script
    assert 'void perform(async () => { await loadTasks(); await loadBoard(); notice(`Loaded ${byId("task-count").textContent}.`); });' in workflow_script
    assert "X-Skybuild-Workbench" in task_script and "X-Skybuild-Workbench" in workflow_script
    assert runtime["private_mode"] is True and runtime["project"] == PROJECT
    assert TOKEN not in task_page + task_script + workflow_page + workflow_script + bootstrap + json.dumps(runtime)
    assert PASSWORD not in login_page.text + task_page + workflow_page + bootstrap
    assert all(request.headers["authorization"] == f"Bearer {TOKEN}" for request in seen)
    assert all(request.url.path.startswith("/api/v1/projects/skybuild/tasks") or
               request.url.path.endswith("/workflow-board") for request in seen)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is needed to execute retained browser scripts")
def test_private_browser_scripts_auto_load_real_records_after_login_without_bearer(tmp_path):
    task = {"task_id": "SKYBUILD-REAL-1", "title": "Real task", "status": "in-progress",
            "phase": "implementation", "next_action": "Review result", "responsible": "owner",
            "revision": 4, "priority": 1, "dependencies": [], "blocked_dependencies": [],
            "acceptance_criteria": [], "architecture_refs": []}
    path = token_file(tmp_path / "token")

    def handler(request):
        if request.url.path.endswith("/workflow-board"):
            places = ("ready", "working", "validating", "integrating", "done", "deferred", "hold")
            return httpx.Response(200, json={"columns": [{"place": place, "count": int(place == "working"),
                "oldest_age_seconds": 0, "unknown_age_count": 0} for place in places],
                "tasks": [{**task, "place": "working", "evidence_freshness": "current"}], "total": 1,
                "ready_dependencies_complete": 0, "ready_dependencies_blocked": 0,
                "unenrolled_count": 0, "next_offset": None})
        if request.url.path.endswith("/tasks"):
            return httpx.Response(200, json=[task])
        return httpx.Response(200, json={})

    with TestClient(private_app(handler, path), base_url="https://localhost:8443") as client:
        assert login(client).status_code == 200
        task_script = client.get("/workbench/assets/tasks.js").text
        workflow_script = client.get("/workbench/assets/live-workflow.js").text
        login_script = client.get("/workbench/assets/private-login.js").text
        private_mode_script = client.get("/workbench/assets/private-mode.js").text
        task_page = client.get("/workbench/tasks").text
        workflow_page = client.get("/workbench/workflow").text
    task_file, workflow_file = tmp_path / "tasks.js", tmp_path / "workflow.js"
    login_file, private_mode_file = tmp_path / "login.js", tmp_path / "private-mode.js"
    task_file.write_text(task_script)
    workflow_file.write_text(workflow_script)
    login_file.write_text(login_script)
    private_mode_file.write_text(private_mode_script)
    assert '<form id="connection-form" hidden' in task_page + workflow_page
    assert 'credentials: "same-origin"' in login_script
    assert 'credentials: "same-origin"' in private_mode_script
    for script in (task_file, workflow_file, login_file, private_mode_file):
        checked = subprocess.run(["node", "--check", str(script)], capture_output=True, text=True, timeout=10)
        assert checked.returncode == 0, checked.stderr

    harness = r'''const assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");
class Element {
  constructor(tag="div") { this.tag=tag; this.children=[]; this.listeners={}; this.dataset={}; this.elements=[]; this.value=""; this.hidden=false; this.disabled=false; this.classList={toggle(){},add(){},remove(){}}; }
  set textContent(v) { this._text=String(v); this.children=[]; }
  get textContent() { return (this._text||"")+this.children.map(x=>x.textContent||"").join(""); }
  addEventListener(k,v) { this.listeners[k]=v; } append(...xs) { this.children.push(...xs); }
  replaceChildren(...xs) { this.children=xs; } setAttribute() {} reset() {}
  closest() { return this; } querySelectorAll() { return this.children.flatMap(x=>[x,...x.querySelectorAll()]); }
}
const task={task_id:"SKYBUILD-REAL-1",title:"Real task",status:"in-progress",phase:"implementation",next_action:"Review result",responsible:"owner",revision:4,priority:1,dependencies:[],blocked_dependencies:[],acceptance_criteria:[],architecture_refs:[]};
async function execute(script, workflow) {
  const elements=new Map(), get=id=>{if(!elements.has(id)) elements.set(id,new Element()); return elements.get(id);};
  global.document={getElementById:get,createElement:tag=>new Element(tag),createTextNode:text=>({textContent:String(text),children:[],querySelectorAll:()=>[]}),querySelectorAll:()=>[],documentElement:{dataset:{}}};
  global.window={SKYBUILD_WORKBENCH_PRIVATE:{enabled:true,project:"skybuild"}};
  const requests=[];
  global.crypto={randomUUID:()=>"browser-operation"};
  global.fetch=async (url,options={})=>{requests.push({url,options});
    assert.equal(options.credentials,"same-origin","private requests include the session cookie");
    assert.equal(options.headers["X-Skybuild-Workbench"],"1");
    assert.equal(Object.keys(options.headers).some(k=>k.toLowerCase()==="authorization"),false);
    if((options.method||"GET")==="POST") return {ok:true,status:200,json:async()=>({})};
    const data=url.includes("workflow-board")?{columns:["ready","working","validating","integrating","done","deferred","hold"].map(place=>({place,count:place==="working"?1:0,oldest_age_seconds:0,unknown_age_count:0})),tasks:[{...task,place:"working",evidence_freshness:"current"}],total:1,ready_dependencies_complete:0,ready_dependencies_blocked:0,unenrolled_count:0,next_offset:null}:[task];
    return {ok:true,status:200,json:async()=>data};
  };
  vm.runInThisContext(fs.readFileSync(script,"utf8"));
  for(let i=0;i<30;i++) await new Promise(resolve=>setImmediate(resolve));
  assert.ok(requests.some(x=>x.url.includes("/tasks?")),"startup fetches tasks");
  assert.match(get("task-count").textContent,/1 tasks? (loaded|shown)/);
  if(workflow){ assert.ok(requests.some(x=>x.url.includes("workflow-board?")),"startup fetches board");
    assert.match(get("task-list").textContent,/SKYBUILD-REAL-1/);
    assert.ok(get("workflow-board").querySelectorAll().some(x=>x.textContent.includes("SKYBUILD-REAL-1")),"board renders task ID");
  } else {
    assert.match(get("task-rows").textContent,/SKYBUILD-REAL-1/);
    get("defer-task").value=task.task_id; get("defer-milestone").value="SKYBUILD-REAL-2"; get("defer-reason").value="Wait";
    get("defer-form").listeners.submit({preventDefault(){}});
    for(let i=0;i<30;i++) await new Promise(resolve=>setImmediate(resolve));
    const mutation=requests.find(x=>x.options.method==="POST"); assert.ok(mutation,"deferred action posts");
    assert.equal(mutation.options.headers["X-Skybuild-Workbench"],"1");
    assert.equal(Object.keys(mutation.options.headers).some(k=>k.toLowerCase()==="authorization"),false);
  }
}
(async()=>{await execute(process.argv[2],false); await execute(process.argv[3],true);})().catch(error=>{console.error(error);process.exit(1);});'''
    harness_file = tmp_path / "private-startup-harness.js"
    harness_file.write_text(harness)
    ran = subprocess.run(["node", str(harness_file), str(task_file), str(workflow_file)],
                         capture_output=True, text=True, timeout=15)
    assert ran.returncode == 0, ran.stderr


def test_private_gateway_limits_routes_project_and_caller_credentials(tmp_path):
    seen = []
    path = token_file(tmp_path / "token")
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={})

    with TestClient(private_app(handler, path), base_url="https://localhost:8443") as client:
        assert client.get("/api/v1/projects/skybuild/tasks", headers={
            "X-Skybuild-Workbench": "1", "Sec-Fetch-Site": "same-origin"}).status_code == 401
        assert login(client).status_code == 200
        intent = {"X-Skybuild-Workbench": "1", "Sec-Fetch-Site": "same-origin"}
        assert client.get("/api/v1/projects/skybuild/tasks", headers=intent).status_code == 200
        assert client.get("/api/v1/projects/skybuild/tasks", headers={**intent,
                           "Origin": "https://evil.invalid"}).status_code == 403
        assert client.get("/api/v1/projects/skybuild/tasks", headers={**intent,
                           "Origin": "https://localhost:8443"}).status_code == 200
        assert client.get("/api/v1/projects/skybuild/tasks/T-1/history", headers=intent).status_code == 200
        assert client.get("/api/v1/projects/skybuild/workflow-board", headers=intent).status_code == 200
        assert client.get("/api/v1/projects/skykeep/tasks").status_code == 404
        assert client.get("/api/v1/projects/skybuild/cord").status_code == 404
        assert client.post("/api/v1/projects/skybuild/tasks/T-1/claim").status_code == 404
        assert client.post("/api/v1/projects/skybuild/tasks/T-1/workflow",
                           json={"event": "claim"}, headers={
                               "Origin": "https://localhost:8443", "X-Skybuild-Workbench": "1"}).status_code == 404
        assert client.post("/api/v1/projects/skybuild/tasks/T-1/split").status_code == 404
        assert client.get("/api/v1/projects/skybuild/tasks/%2e%2e/cord").status_code == 404
        assert client.get("/api/v1/projects/skybuild/tasks?limit=101",
                          headers={"X-Skybuild-Workbench": "1"}).status_code == 404
        assert client.get("/api/v1/projects/skybuild/tasks", headers={
            "Authorization": "Bearer caller-token", "X-Skybuild-Workbench": "1"}).status_code == 403
        assert client.get("/api/v1/projects/skybuild/cord").status_code == 404
        assert client.get("/api/v1/projects/skybuild/tasks", headers={"Host": "evil.invalid",
                                                                       "X-Skybuild-Workbench": "1"}).status_code == 400

    assert len(seen) == 4
    assert all(request.headers["authorization"] == f"Bearer {TOKEN}" for request in seen)


def test_private_mode_preserves_caller_authenticated_api_and_never_injects_server_token(tmp_path):
    seen = []
    path = token_file(tmp_path / "token")

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    with TestClient(private_app(handler, path), base_url="https://localhost:8443") as client:
        assert client.get("/api/v1/me", headers={"Authorization": "Bearer worker-token"}).status_code == 200
        assert client.get("/api/v1/projects/skybuild/cord", headers={
            "Authorization": "Bearer operator-token"}).status_code == 200
        assert client.get("/api/v1/projects/skybuild/cord").status_code == 404
    assert [request.headers["authorization"] for request in seen] == [
        "Bearer worker-token", "Bearer operator-token"]
    assert all(TOKEN not in request.headers["authorization"] for request in seen)


def test_private_mode_keeps_uncredentialed_health_routes(tmp_path):
    seen = []
    path = token_file(tmp_path / "token")
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"status": "ready"})
    with TestClient(private_app(handler, path), base_url="https://localhost:8443") as client:
        assert client.get("/health/ready").json() == {"status": "ready"}
        assert client.get("/health/unrelated").status_code == 404
    assert len(seen) == 1
    assert "authorization" not in seen[0].headers


@pytest.mark.parametrize("headers", [
    {"X-Skybuild-Workbench": "1"},
    {"Origin": "https://evil.invalid", "X-Skybuild-Workbench": "1"},
    {"Origin": "https://localhost:8443"},
    {"Origin": "https://localhost:8443", "X-Skybuild-Workbench": "1", "Sec-Fetch-Site": "cross-site"},
])
def test_private_mutation_requires_exact_same_origin_and_browser_intent(tmp_path, headers):
    path = token_file(tmp_path / "token")
    with TestClient(private_app(lambda request: pytest.fail("rejected mutation reached backend"), path),
                    base_url="https://localhost:8443") as client:
        assert login(client).status_code == 200
        response = client.post("/api/v1/projects/skybuild/tasks/T-1/actions/defer",
                               json={"reason": "later"}, headers=headers)
    assert response.status_code == 403


def test_private_mutations_preserve_idempotency_and_revision_headers(tmp_path):
    seen = []
    path = token_file(tmp_path / "token")
    def handler(request):
        seen.append(request)
        return httpx.Response(409, json={"detail": "conflict"})

    with TestClient(private_app(handler, path), base_url="https://localhost:8443") as client:
        response = client.patch("/api/v1/projects/skybuild/tasks/T-1", content=b'{"title":"x"}', headers={
            "Origin": "https://localhost:8443", "X-Skybuild-Workbench": "1",
            "Sec-Fetch-Site": "same-origin", "If-Match": "9", "Idempotency-Key": "stable-key",
            "Authorization": "Bearer caller-token"})
    assert response.status_code == 403
    assert not seen

    with TestClient(private_app(handler, path), base_url="https://localhost:8443") as client:
        assert login(client).status_code == 200
        response = client.patch("/api/v1/projects/skybuild/tasks/T-1", content=b'{"title":"x"}', headers={
            "Origin": "https://localhost:8443", "X-Skybuild-Workbench": "1",
            "Sec-Fetch-Site": "same-origin", "If-Match": "9", "Idempotency-Key": "stable-key"})
    assert response.status_code == 409
    assert seen[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert seen[0].headers["if-match"] == "9" and seen[0].headers["idempotency-key"] == "stable-key"


def test_private_task_crud_actions_and_workflow_routes_inject_server_credential(tmp_path):
    seen = []
    path = token_file(tmp_path / "token")
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={})

    headers = {"Origin": "https://localhost:8443", "X-Skybuild-Workbench": "1",
               "Sec-Fetch-Site": "same-origin", "Idempotency-Key": "stable"}
    with TestClient(private_app(handler, path), base_url="https://localhost:8443") as client:
        assert login(client).status_code == 200
        assert client.post("/api/v1/projects/skybuild/tasks", json={"task_id": "T-2"}, headers=headers).status_code == 200
        assert client.post("/api/v1/projects/skybuild/tasks/T-1/actions/defer",
                           json={"reason": "later"}, headers={**headers, "If-Match": "4"}).status_code == 200
        assert client.post("/api/v1/projects/skybuild/tasks/T-1/workflow",
                           json={"event": "hold", "reason": "pause"}, headers={**headers, "If-Match": "5"}).status_code == 200
    assert len(seen) == 3
    assert all(item.headers["authorization"] == f"Bearer {TOKEN}" for item in seen)
    assert seen[1].headers["if-match"] == "4" and seen[2].headers["if-match"] == "5"


def test_private_mode_rejects_missing_or_unsafe_credential_configuration(tmp_path):
    with pytest.raises(ValueError, match="requires token, project, username, and password file"):
        runtime_ui.create_app(ui_checkout=ROOT / "ui", api_checkout=API, ca_file=CA,
                              backend_hostname=HOST, workbench_token_file=tmp_path / "missing")
    missing = tmp_path / "missing"
    with pytest.raises(ValueError, match="unavailable or invalid"):
        private_app(lambda request: httpx.Response(200), missing)
    malformed = token_file(tmp_path / "malformed", "too-short")
    with pytest.raises(ValueError, match="unavailable or invalid"):
        private_app(lambda request: httpx.Response(200), malformed)
    secret = token_file(tmp_path / "secret")
    link = tmp_path / "link"
    link.symlink_to(secret)
    with pytest.raises(ValueError, match="unavailable or invalid"):
        private_app(lambda request: httpx.Response(200), link)
    secret.chmod(0o644)
    with pytest.raises(ValueError, match="protected regular file"):
        private_app(lambda request: httpx.Response(200), secret)
    secret.chmod(0o600)
    with pytest.raises(ValueError, match="Invalid private Workbench project"):
        private_app(lambda request: httpx.Response(200), secret, "../skybuild")

    password = token_file(tmp_path / "password", "private-test-password")
    password.chmod(0o644)
    with pytest.raises(ValueError, match="password file is not a protected regular file"):
        runtime_ui.create_app(ui_checkout=ROOT / "ui", api_checkout=API, ca_file=CA,
                              backend_hostname=HOST, workbench_token_file=secret,
                              workbench_project=PROJECT, workbench_username=USERNAME,
                              workbench_password_file=password)


def test_private_gateway_redacts_backend_credential_echoes(tmp_path):
    path = token_file(tmp_path / "token")
    def handler(request):
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        return httpx.Response(401, content=f"echo {TOKEN}".encode(), headers={"ETag": TOKEN})

    with TestClient(private_app(handler, path), base_url="https://localhost:8443") as client:
        assert login(client).status_code == 200
        response = client.get("/api/v1/projects/skybuild/tasks", headers={"X-Skybuild-Workbench": "1"})
    assert response.status_code == 401
    assert TOKEN not in response.text
    assert TOKEN not in response.headers.get("etag", "")

    def fail_with_secret(request):
        raise httpx.ConnectError(f"upstream rejected Bearer {TOKEN}", request=request)
    with TestClient(private_app(fail_with_secret, path), base_url="https://localhost:8443") as client:
        assert login(client).status_code == 200
        failure = client.get("/api/v1/projects/skybuild/tasks", headers={"X-Skybuild-Workbench": "1"})
    assert failure.status_code == 502
    assert TOKEN not in failure.text


def test_default_gateway_keeps_bearer_behavior_without_private_mode():
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=[])
    with TestClient(runtime_ui.create_app(ui_checkout=ROOT / "ui", api_checkout=API, ca_file=CA,
                    backend_hostname=HOST, backend_connect_host="api", transport=httpx.MockTransport(handler)),
                    base_url="https://localhost:8443") as client:
        response = client.get("/api/v1/projects/skybuild/tasks", headers={"Authorization": "Bearer caller-token"})
        runtime = client.get("/workbench/runtime").json()
        private_bootstrap = client.get("/workbench/assets/private-mode.js")
    assert response.status_code == 200
    assert seen[0].headers["authorization"] == "Bearer caller-token"
    assert runtime["private_mode"] is False
    assert private_bootstrap.status_code == 404


@pytest.mark.parametrize(("host", "tls", "container", "private", "allowed"), [
    ("127.0.0.1", False, False, True, False),
    ("100.80.1.2", True, False, True, True),
    ("fd7a:115c:a1e0::12", True, False, True, True),
    ("0.0.0.0", True, True, True, True),
    ("0.0.0.0", True, False, True, False),
    ("192.0.2.4", True, False, True, False),
    ("api.example.test", True, False, True, False),
    ("api.example.test", True, False, False, True),
])
def test_listener_boundary_for_private_mode(host, tls, container, private, allowed):
    assert (runtime_ui._listener_error(host, tls, container, private) is None) is allowed
