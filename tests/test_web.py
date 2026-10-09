from pathlib import Path
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

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
  dependencies: [" dep ", "dep"], acceptance_criteria: ["one", "two"], architecture_refs: ["ref"], revision: 7};
const other = {...task, task_id: "other", revision: 3, acceptance_criteria: ["three"], dependencies: ["dep"]};
const requests = [];
global.fetch = async (url, options) => {
  requests.push({url, options});
  const data = url.endsWith("/split") ? {children: [{task_id: "child"}]} :
    url.endsWith("/tasks/merge") ? {target: {task_id: "combined"}} :
    url.includes("/history?") ? [] : url.includes("tasks?") ? [task] :
    url.endsWith("/tasks/other") ? other : task;
  return {ok: true, status: 200, json: async () => data};
};
vm.runInThisContext(fs.readFileSync(process.argv[1], "utf8"));
const tick = () => new Promise(resolve => setImmediate(resolve));
async function run() {
  get("project").value = " project "; get("token").value = "test-token";
  get("connection-form").listeners.submit({preventDefault() {}}); await tick();
  get("task-list").querySelectorAll()[0].listeners.click(); await tick();
  get("edit-title").value = "Renamed";
  get("edit-dependencies").value = " dep \r\ndep\r\n   \r\n\t\r\n";
  get("edit-form").listeners.submit({preventDefault() {}}); await tick();
  const patches = requests.filter(request => request.options.method === "PATCH");
  assert.equal(patches.length, 1);
  assert.deepEqual(JSON.parse(patches[0].options.body).dependencies, [" dep ", "dep"]);
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
}
run().catch(error => { console.error(error); process.exitCode = 1; });
'''
    result = subprocess.run([shutil.which("node"), "-e", harness, str(script)], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stdout + result.stderr
