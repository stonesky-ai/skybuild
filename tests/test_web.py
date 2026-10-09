from pathlib import Path
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient
from fastapi import FastAPI

from skybuild.api import create_app


class NoStoreAccess:
    def __getattr__(self, name):
        raise AssertionError(f"Public browser assets must not access Store.{name}")


def workbench():
    app = create_app(NoStoreAccess())
    return TestClient(app)


def test_workbench_and_fixed_assets_have_security_headers_without_db_access():
    with workbench() as client:
        for path, content_type in (
            ("/workbench", "text/html"),
            ("/workbench/assets/workbench.js", "text/javascript"),
            ("/workbench/assets/workbench.css", "text/css"),
        ):
            response = client.get(path)
            assert response.status_code == 200
            assert response.headers["content-type"].startswith(content_type)
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["referrer-policy"] == "no-referrer"
            assert response.headers["x-content-type-options"] == "nosniff"
            policy = response.headers["content-security-policy"]
            assert "default-src 'self'" in policy
            assert "connect-src 'self'" in policy
            assert "frame-ancestors 'none'" in policy
            assert "unsafe-inline" not in policy


def test_public_page_contains_no_private_tasks_and_assets_cannot_be_overridden():
    with workbench() as client:
        page = client.get("/workbench").text
        assert 'type="password"' in page
        assert 'id="task-list" class="task-list"></ul>' in page
        assert 'id="history" class="history"></ol>' in page
        assert "ledgers remain authoritative" in page
        expected = client.get("/workbench/assets/workbench.js").content
        assert client.get("/workbench/assets/workbench.js?path=/etc/passwd&content_type=text/html").content == expected
        for path in (
            "/workbench/assets/api.py", "/workbench/assets/../../api.py",
            "/workbench/assets/%2e%2e/api.py", "/workbench/assets/%2e%2e%2fapi.py",
            "/workbench/assets/workbench.html", "/workbench/assets/.env",
        ):
            assert client.get(path).status_code == 404


def test_copied_status_views_are_inside_workbench_and_have_safe_headers():
    with workbench() as client:
        page = client.get("/workbench/views/all")
        assert page.status_code == 200
        assert page.headers["content-type"].startswith("text/html")
        assert 'href="/workbench"' in page.text
        assert 'href="/workbench#create-title"' in page.text
        assert 'href="/workbench#history-title"' in page.text
        assert "Ideas" not in page.text
        assert "/ideas" not in page.text
        assert 'href="/workbench/assets/workbench-shell.css"' in page.text
        assert 'href="/workbench/views/queue#section-queue"' in page.text
        assert 'href="/workbench/views/all#section-flow"' in page.text
        policy = page.headers["content-security-policy"]
        assert "connect-src 'self'" in policy
        assert "style-src 'self'" in policy
        assert "unsafe-inline" not in policy

        state = client.get("/workbench/api/state")
        assert state.status_code == 200
        assert state.json()["sections"]["preview"]["status"] == "SkyKeep tools page copied into SkyBuild"
        assert client.get("/workbench/views/queue").status_code == 200
        assert client.get("/workbench/queue.html").status_code == 200
        assert client.get("/workbench/assets/source/page.py").status_code == 404
        assert client.get("/ideas").status_code == 404


def test_development_preview_exposes_browser_reload_revision():
    from skybuild.web import install_workbench

    app = FastAPI()
    install_workbench(app, dev_reload=True)
    with TestClient(app) as client:
        page = client.get("/workbench")
        assert page.status_code == 200
        assert "/workbench/dev/reload.js?revision=" in page.text
        revision = client.get("/workbench/dev/revision")
        assert revision.status_code == 200
        assert len(revision.json()["revision"]) == 16
        script = client.get("/workbench/dev/reload.js", params={"revision": revision.json()["revision"]})
        assert script.status_code == 200
        assert "location.reload()" in script.text
        assert script.headers["content-type"].startswith("text/javascript")


@pytest.mark.parametrize("path", [
    "/workbench/api/alarms/ack", "/workbench/api/summary",
    "/workbench/api/cleanup/plan", "/workbench/api/cleanup/run",
])
def test_status_view_mutation_routes_are_not_mounted(path):
    with workbench() as client:
        assert client.post(path, json={}).status_code == 404


def test_browser_source_excludes_persistent_token_storage_and_html_injection_sinks():
    with workbench() as client:
        script = client.get("/workbench/assets/workbench.js").text
        for forbidden in ("localStorage", "sessionStorage", "document.cookie", "innerHTML", "outerHTML", "insertAdjacentHTML"):
            assert forbidden not in script


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is needed for the browser handler regression")
def test_browser_handlers_preserve_ids_and_preview_structural_mapping():
    script = Path(__file__).parents[1] / "src" / "skybuild" / "static" / "workbench.js"
    # Execute the actual event handlers against a small DOM/transport boundary.
    harness = r'''
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
class Element {
  constructor(tag = "input") {
    this.tag = tag; this.value = ""; this.children = []; this.elements = [];
    this.dataset = {}; this.listeners = {}; this.classList = { toggle() {} };
  }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  setAttribute() {}
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  querySelectorAll() {
    return this.children.flatMap(child => [
      ...(child.tag === "button" ? [child] : []), ...child.querySelectorAll()
    ]);
  }
  reset() {}
}
const elements = new Map();
const get = id => {
  if (!elements.has(id)) elements.set(id, new Element());
  return elements.get(id);
};
global.document = { getElementById: get, createElement: tag => new Element(tag) };
const task = {task_id: " task ", title: "Title", description: "Brief", status: "proposed",
  phase: "triage", next_action: "Review", blocker: null, responsible: "owner",
  dependencies: [" dep ", "dep"], acceptance_criteria: ["one\ntwo"], architecture_refs: ["ref\nsection"], revision: 7};
const other = {...task, task_id: "other", revision: 3, acceptance_criteria: ["three"], dependencies: ["dep"]};
const requests = [];
let endPage = false;
global.fetch = async (url, options) => {
  requests.push({url, options});
  const data = url.includes("/reconcile-due?") ? (url.includes("after_task_id=") ?
      {scanned: 1, reassessed: ["due"], next_after_task_id: null} :
      {scanned: 100, reassessed: [], next_after_task_id: "cursor-100"}) :
    url.endsWith("/split") ? {children: [{task_id: "child"}]} :
    url.endsWith("/tasks/merge") ? {target: {task_id: "combined"}} :
    url.includes("/history?") ? (url.includes("offset=100") ? [{revision: 101, actor: "owner", operation: "updated"}] :
      Array.from({length: 100}, (_, index) => ({revision: index + 1, actor: "owner", operation: "updated"}))) :
    url.endsWith("/lineage") ?
      [{source_task_id: " task ", target_task_id: "<child>", action: "split"}] :
    url.includes("tasks?") ? (url.includes("after_task_id=") ? (endPage ? [] : [other]) :
      [task, ...Array.from({length: 99}, (_, index) => ({...task, task_id: `filler-${String(index).padStart(3, "0")}`}))]) :
    url.endsWith("/tasks/other") ? other : task;
  return {ok: true, status: 200, json: async () => data};
};
vm.runInThisContext(fs.readFileSync(process.argv[1], "utf8"));
const tick = () => new Promise(resolve => setImmediate(resolve));
async function run() {
  get("project").value = " project "; get("token").value = "test-token";
  get("connection-form").listeners.submit({preventDefault() {}}); await tick();
  get("reconcile-due").listeners.click(); await tick();
  assert.ok(requests.some(request => request.url.includes("/reconcile-due?limit=100&after_task_id=cursor-100")));
  assert.match(get("task-list").querySelectorAll()[0].textContent, /proposed · triage/);
  assert.match(get("task-list").querySelectorAll()[0].textContent, /Review · owner/);
  get("task-list").querySelectorAll()[0].listeners.click(); await tick();
  assert.equal(get("history").children.length, 100);
  assert.equal(get("load-more-history").disabled, false);
  get("load-more-history").listeners.click(); await tick();
  assert.equal(get("history").children.length, 101);
  assert.equal(get("load-more-history").disabled, true);
  assert.match(get("full-task-record").textContent, /"task_id": " task "/);
  assert.match(get("full-task-record").textContent, /"acceptance_criteria": \[/);
  assert.equal(get("lineage").children[0].textContent, " task  → <child> (split)");
  assert.ok(requests.some(request => request.url.endsWith("/tasks/%20task%20/lineage")));
  assert.deepEqual(JSON.parse(get("edit-acceptance").value), ["one\ntwo"]);
  assert.deepEqual(JSON.parse(get("edit-architecture").value), ["ref\nsection"]);
  get("edit-title").value = "Renamed";
  get("edit-dependencies").value = " dep \r\ndep\r\n   \r\n\t\r\n";
  get("edit-acceptance").value = "not JSON";
  get("edit-form").listeners.submit({preventDefault() {}}); await tick();
  assert.equal(requests.filter(request => request.options.method === "PATCH").length, 0);
  get("edit-acceptance").value = JSON.stringify(["one\ntwo"]);
  get("edit-form").listeners.submit({preventDefault() {}}); await tick();
  const patches = requests.filter(request => request.options.method === "PATCH");
  assert.equal(patches.length, 1);
  assert.deepEqual(JSON.parse(patches[0].options.body).dependencies, [" dep ", "dep"]);
  assert.deepEqual(JSON.parse(patches[0].options.body).acceptance_criteria, ["one\ntwo"]);
  assert.deepEqual(JSON.parse(patches[0].options.body).architecture_refs, ["ref\nsection"]);
  assert.equal(patches[0].options.headers["If-Match"], "7");
  assert.ok(patches[0].url.endsWith("/tasks/%20task%20"));
  assert.ok(requests.every(request => request.url.startsWith("/api/v1/projects/%20project%20/")));
  const plan = {reason: "Separate scope", children: [
    {task_id: " child ", title: "One", description: "One", acceptance_criteria: ["one"], dependencies: [" dep "]},
    {task_id: "child-two", title: "Two", description: "Two", acceptance_criteria: ["two"], dependencies: []}
  ], incoming: {" dependent ": [" child "]}};
  get("structure-kind").value = "split";
  get("structure-plan").value = JSON.stringify(plan);
  get("preview-structure").listeners.click(); await tick();
  assert.match(get("structure-preview").textContent, /dependent/);
  assert.match(get("structure-preview").textContent, /" dependent ": \[\s*" child "\s*\]/);
  assert.match(get("structure-preview").textContent, /"description": "One"/);
  assert.equal(get("apply-structure").disabled, false);
  get("structure-plan").value = JSON.stringify({...plan, incoming: {" dependent ": ["child-two"]}});
  get("structure-plan").listeners.input();
  assert.equal(get("apply-structure").disabled, true);
  get("structure-form").listeners.submit({preventDefault() {}}); await tick();
  assert.equal(requests.filter(request => request.url.endsWith("/split")).length, 0);
  get("structure-plan").value = JSON.stringify(plan);
  get("preview-structure").listeners.click(); await tick();
  get("structure-form").listeners.submit({preventDefault() {}}); await tick();
  const splits = requests.filter(request => request.url.endsWith("/tasks/%20task%20/split"));
  assert.equal(splits.length, 1);
  assert.deepEqual(JSON.parse(splits[0].options.body).incoming, {" dependent ": [" child "]});
  assert.equal(splits[0].options.headers["If-Match"], "7");
  const merge = {reason: "Combine scope", source_task_ids: [" task ", "other"],
    expected_revisions: {" task ": 7, other: 3},
    target: {task_id: "combined", title: "Combined", description: "All scope",
      acceptance_criteria: ["one", "two", "three"], architecture_refs: ["ref"], dependencies: [" dep ", "dep"]},
    incoming_dependents: [" dependent "]};
  get("structure-kind").value = "merge";
  get("structure-plan").value = JSON.stringify(merge);
  get("preview-structure").listeners.click(); await tick();
  assert.match(get("structure-preview").textContent, /"three"/);
  assert.match(get("structure-preview").textContent, /"description": "All scope"/);
  assert.match(get("structure-preview").textContent, /" dependent "/);
  assert.equal(get("apply-structure").disabled, false);
  assert.equal(get("next-tasks").disabled, false);
  get("next-tasks").listeners.click(); await tick();
  assert.equal(get("task-list").querySelectorAll().length, 1);
  assert.ok(requests.some(request => request.url.includes("after_task_id=filler-098")));
  assert.equal(get("next-tasks").disabled, true);
  assert.equal(get("first-tasks").disabled, false);
  get("first-tasks").listeners.click(); await tick();
  assert.equal(get("task-list").querySelectorAll().length, 100);
  endPage = true;
  get("next-tasks").listeners.click(); await tick();
  assert.equal(get("task-list").querySelectorAll().length, 100);
  assert.equal(get("next-tasks").disabled, true);
  get("logout").listeners.click();
  assert.equal(get("full-task-record").textContent, "No task selected.");
  assert.equal(get("lineage").children.length, 0);
}
run().catch(error => { console.error(error); process.exitCode = 1; });
'''
    result = subprocess.run([shutil.which("node"), "-e", harness, str(script)], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stdout + result.stderr
