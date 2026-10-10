import json
import importlib.util
from pathlib import Path

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
        bootstrap = client.get("/workbench/assets/private-mode.js").text
        runtime = client.get("/workbench/runtime").json()

    assert 'private-mode.js' in task_page and 'private-mode.js' in workflow_page
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


def test_private_gateway_limits_routes_project_and_caller_credentials(tmp_path):
    seen = []
    path = token_file(tmp_path / "token")
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={})

    with TestClient(private_app(handler, path), base_url="https://localhost:8443") as client:
        intent = {"X-Skybuild-Workbench": "1", "Sec-Fetch-Site": "same-origin"}
        assert client.get("/api/v1/projects/skybuild/tasks", headers=intent).status_code == 200
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
        assert client.get("/api/v1/projects/skybuild/tasks", headers={"Authorization": "Bearer caller-token"}).status_code == 403
        assert client.get("/api/v1/projects/skybuild/tasks", headers={"Host": "evil.invalid",
                                                                       "X-Skybuild-Workbench": "1"}).status_code == 400

    assert len(seen) == 3
    assert all(request.headers["authorization"] == f"Bearer {TOKEN}" for request in seen)


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
