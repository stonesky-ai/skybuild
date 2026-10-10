import json
import importlib.util
from pathlib import Path
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


def token_file(path: Path, value: str = TOKEN) -> Path:
    path.write_text(value + "\n")
    path.chmod(0o600)
    return path


def private_app(handler, token_path, project=PROJECT):
    return runtime_ui.create_app(
        ui_checkout=ROOT / "ui", api_checkout=API, ca_file=CA,
        backend_hostname=HOST, backend_connect_host="api",
        workbench_token_file=token_path, workbench_project=project,
        transport=httpx.MockTransport(handler),
    )


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
        task_page = client.get("/workbench/tasks").text
        task_script = client.get("/workbench/assets/tasks.js").text
        workflow_page = client.get("/workbench/workflow").text
        workflow_script = client.get("/workbench/assets/live-workflow.js").text
        private_style = client.get("/workbench/assets/private-mode.css")
        bootstrap = client.get("/workbench/assets/private-mode.js").text
        runtime = client.get("/workbench/runtime").json()

    assert 'private-mode.js' in task_page and 'private-mode.js' in workflow_page
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
    assert all(request.headers["authorization"] == f"Bearer {TOKEN}" for request in seen)
    assert all(request.url.path.startswith("/api/v1/projects/skybuild/tasks") or
               request.url.path.endswith("/workflow-board") for request in seen)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is needed to execute retained browser scripts")
def test_private_browser_scripts_auto_load_real_records_without_login_or_bearer(tmp_path):
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
        task_script = client.get("/workbench/assets/tasks.js").text
        workflow_script = client.get("/workbench/assets/live-workflow.js").text
        task_page = client.get("/workbench/tasks").text
        workflow_page = client.get("/workbench/workflow").text
    task_file, workflow_file = tmp_path / "tasks.js", tmp_path / "workflow.js"
    task_file.write_text(task_script)
    workflow_file.write_text(workflow_script)
    assert '<form id="connection-form" hidden' in task_page + workflow_page
    for script in (task_file, workflow_file):
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
        assert client.post("/api/v1/projects/skybuild/tasks", json={"task_id": "T-2"}, headers=headers).status_code == 200
        assert client.post("/api/v1/projects/skybuild/tasks/T-1/actions/defer",
                           json={"reason": "later"}, headers={**headers, "If-Match": "4"}).status_code == 200
        assert client.post("/api/v1/projects/skybuild/tasks/T-1/workflow",
                           json={"event": "hold", "reason": "pause"}, headers={**headers, "If-Match": "5"}).status_code == 200
    assert len(seen) == 3
    assert all(item.headers["authorization"] == f"Bearer {TOKEN}" for item in seen)
    assert seen[1].headers["if-match"] == "4" and seen[2].headers["if-match"] == "5"


def test_private_mode_rejects_missing_or_unsafe_credential_configuration(tmp_path):
    with pytest.raises(ValueError, match="requires both"):
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


def test_private_gateway_redacts_backend_credential_echoes(tmp_path):
    path = token_file(tmp_path / "token")
    def handler(request):
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        return httpx.Response(401, content=f"echo {TOKEN}".encode(), headers={"ETag": TOKEN})

    with TestClient(private_app(handler, path), base_url="https://localhost:8443") as client:
        response = client.get("/api/v1/projects/skybuild/tasks", headers={"X-Skybuild-Workbench": "1"})
    assert response.status_code == 401
    assert TOKEN not in response.text
    assert TOKEN not in response.headers.get("etag", "")

    def fail_with_secret(request):
        raise httpx.ConnectError(f"upstream rejected Bearer {TOKEN}", request=request)
    with TestClient(private_app(fail_with_secret, path), base_url="https://localhost:8443") as client:
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
    ("127.0.0.1", False, False, True, True),
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
