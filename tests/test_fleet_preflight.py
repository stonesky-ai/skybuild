"""A worker preflight sees its own narrow identity without sending a task."""

import json
import os
import subprocess
import sys

import httpx
import pytest

from skybuild import fleet_preflight
from skybuild.fleet_preflight import PreflightError, probe_private_api
from skybuild.workflow import Place, TaskToken


URL = "https://jeltz.tail991ac1.ts.net"
TOKEN = "worker-secret-material-for-a-disposable-test"


def checked_probe(*args, **kwargs):
    return probe_private_api(*args, resolve=lambda host: ["100.95.249.118"], **kwargs)


@pytest.fixture
def token_file(tmp_path):
    path = tmp_path / "worker.token"
    path.write_text(TOKEN + "\n")
    path.chmod(0o600)
    return path


def workflow_view(*, project_id="skybuild", task_id="TASK-1", revision=7,
                  place=Place.WORKING, description="private task description"):
    token = TaskToken(project_id=project_id, task_id=task_id, revision=revision,
                      input_generation=9, definition_revision=5,
                      policy_version="petri-checks-v1", place=place).to_dict()
    return {"task": {"project_id": project_id, "task_id": task_id,
                     "revision": revision, "description": description},
            "token": token, "available_actions": [], "transitions": [],
            "disabled_actions": {}}


def transport_for(identity, *, ready=True, tasks=None, task_status=200,
                  selected_workflow=None, requests=None):
    def handle(request):
        if requests is not None:
            requests.append(request)
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert request.method == "GET"
        if request.url.path == "/health/ready":
            return httpx.Response(200 if ready else 503, json={"status": "ready" if ready else "unavailable"})
        if request.url.path == "/api/v1/me":
            return httpx.Response(200, json=identity)
        if request.url.path == "/api/v1/projects/skybuild/cord/inbox":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/v1/projects/skybuild/tasks":
            assert request.url.params["limit"] == "1"
            assert request.url.params["by_id"] == "true"
            return httpx.Response(task_status, json=[] if tasks is None else tasks)
        if request.url.path == "/api/v1/projects/skybuild/tasks/TASK-1/workflow":
            assert request.url.query == b""
            if selected_workflow is None:
                return httpx.Response(200, json=workflow_view())
            return httpx.Response(200, json=selected_workflow)
        raise AssertionError("Unexpected request")
    return httpx.MockTransport(handle)


def identity(**overrides):
    return {"principal_id": "wonko-worker", "is_admin": False,
            "grants": {"skybuild": ["tasks:read", "cord:read", "cord:send", "cord:handle"]}} | overrides


def test_private_ready_project_scoped_worker_can_read_inbox(token_file):
    result = checked_probe(URL, "skybuild", token_file, "wonko-worker",
                               transport=transport_for(identity()))
    assert result == {"ready": True, "project_id": "skybuild", "principal_id": "wonko-worker",
                      "scopes": ["cord:handle", "cord:read", "cord:send", "tasks:read"],
                      "inbox_access": True, "task_list_access": True,
                      "host": "jeltz.tail991ac1.ts.net"}
    assert TOKEN not in json.dumps(result)


def test_petri_worker_profile_requires_explicit_selection_and_exact_grants(token_file):
    grants = {"skybuild": ["tasks:read", "tasks:claim", "tasks:write", "cord:read", "cord:send", "cord:handle"]}
    with pytest.raises(PreflightError):
        checked_probe(URL, "skybuild", token_file, "wonko-worker", transport=transport_for(identity(grants=grants)))
    assert checked_probe(URL, "skybuild", token_file, "wonko-worker", workflow=True,
                         transport=transport_for(identity(grants=grants)))["ready"] is True
    with pytest.raises(PreflightError):
        checked_probe(URL, "skybuild", token_file, "wonko-worker", workflow=True, transport=transport_for(identity()))


@pytest.mark.parametrize("overrides", [
    {"principal_id": "another-worker"},
    {"is_admin": True},
    {"grants": {"skybuild": ["cord:read", "cord:send", "cord:handle", "tasks:write"]}},
    {"grants": {"skybuild": ["cord:read", "cord:send"]}},
    {"grants": {"skybuild": ["cord:read", "cord:send", "cord:handle"], "other": ["tasks:read"]}},
    {"grants": {"skybuild": [{}]}},
])
def test_wrong_identity_or_broad_or_missing_grants_refused(token_file, overrides):
    with pytest.raises(PreflightError):
        checked_probe(URL, "skybuild", token_file, "wonko-worker",
                          transport=transport_for(identity(**overrides)))


def test_not_ready_or_public_url_or_unsafe_token_refused(token_file):
    with pytest.raises(PreflightError):
        checked_probe(URL, "skybuild", token_file, "wonko-worker",
                          transport=transport_for(identity(), ready=False))
    with pytest.raises(PreflightError, match="Tailscale"):
        checked_probe("https://example.com", "skybuild", token_file, "wonko-worker")
    token_file.chmod(0o644)
    with pytest.raises(PreflightError, match="private regular file"):
        checked_probe(URL, "skybuild", token_file, "wonko-worker")


def test_public_dns_target_refused_before_token_use(token_file):
    with pytest.raises(PreflightError, match="Tailscale addresses"):
        probe_private_api(URL, "skybuild", token_file, "wonko-worker",
                          resolve=lambda host: ["93.184.215.14"])


def test_token_symlink_refused(token_file, tmp_path):
    link = tmp_path / "link.token"
    link.symlink_to(token_file)
    with pytest.raises(PreflightError, match="cannot be read safely"):
        checked_probe(URL, "skybuild", link, "wonko-worker")


def test_token_fifo_is_refused_without_blocking(tmp_path):
    fifo = tmp_path / "worker.token"
    os.mkfifo(fifo, 0o600)
    result = subprocess.run(
        [sys.executable, "-c", "from pathlib import Path; import sys; "
         "from skybuild.fleet_preflight import _token_from_file, PreflightError; "
         "\ntry: _token_from_file(Path(sys.argv[1]))\nexcept PreflightError: print('rejected')", str(fifo)],
        capture_output=True, text=True, timeout=2, check=False,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "rejected"


@pytest.mark.parametrize("tasks, status", [({}, 200), ([{}, {}], 200),
                                              ({"error": {"code": "authorization"}}, 403)])
def test_task_list_must_be_accessible_and_bounded(token_file, tasks, status):
    with pytest.raises(PreflightError):
        checked_probe(URL, "skybuild", token_file, "wonko-worker",
                      transport=transport_for(identity(), tasks=tasks, task_status=status))


def test_selected_workflow_is_read_only_and_checks_pinned_inputs(token_file):
    requests = []
    result = checked_probe(
        URL, "skybuild", token_file, "wonko-worker", task_id="TASK-1",
        expected_task_revision=7, expected_input_generation=9,
        expected_definition_revision=5, expected_policy_version="petri-checks-v1",
        transport=transport_for(identity(), selected_workflow=workflow_view(), requests=requests))
    assert result["workflow_probe"] == {
        "task_id": "TASK-1", "revision": 7, "input_generation": 9,
        "definition_revision": 5, "policy_version": "petri-checks-v1",
        "place": "working", "compatible": True, "execution_authorized": False}
    assert [request.method for request in requests] == ["GET"] * 5
    assert requests[-1].url.path.endswith("/tasks/TASK-1/workflow")
    encoded = json.dumps(result)
    assert TOKEN not in encoded
    assert "private task description" not in encoded


@pytest.mark.parametrize(("field", "expected"), [
    ("expected_task_revision", 8),
    ("expected_input_generation", 10),
    ("expected_definition_revision", 6),
    ("expected_policy_version", "other-policy"),
])
def test_selected_workflow_rejects_stale_expected_input(token_file, field, expected):
    with pytest.raises(PreflightError, match="differs from expected inputs"):
        checked_probe(URL, "skybuild", token_file, "wonko-worker", task_id="TASK-1",
                      transport=transport_for(identity(), selected_workflow=workflow_view()),
                      **{field: expected})


@pytest.mark.parametrize("view", [
    {},
    {"task": {}, "token": {"project_id": "skybuild", "task_id": "TASK-1"},
     "available_actions": [], "transitions": [], "disabled_actions": {}},
    workflow_view(project_id="another-project"),
    workflow_view(task_id="ANOTHER-TASK"),
    workflow_view(revision=8) | {"task": {"project_id": "skybuild", "task_id": "TASK-1", "revision": 7}},
    workflow_view() | {"token": workflow_view()["token"] | {"place": "future-place"}},
])
def test_selected_workflow_rejects_absent_malformed_or_wrong_identity(token_file, view):
    with pytest.raises(PreflightError):
        checked_probe(URL, "skybuild", token_file, "wonko-worker", task_id="TASK-1",
                      transport=transport_for(identity(), selected_workflow=view))


def test_workflow_expectations_require_selected_task_before_network(token_file):
    with pytest.raises(PreflightError, match="require a selected task"):
        probe_private_api(URL, "skybuild", token_file, "wonko-worker",
                          expected_input_generation=1,
                          resolve=lambda host: pytest.fail("DNS should not run"),
                          transport=httpx.MockTransport(lambda request: pytest.fail("Unexpected request")))


def test_cli_failure_does_not_print_secret_or_task_description(token_file, monkeypatch, capsys):
    monkeypatch.setattr(fleet_preflight, "_resolved_addresses", lambda host: ["100.95.249.118"])

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def request(self, method, path):
            return {"status": "ready"}

        def whoami(self):
            return identity()

        def inbox(self, project, limit):
            return []

        def list_tasks(self, project, **kwargs):
            return []

        def task_workflow(self, project, task):
            return workflow_view(description=TOKEN)

    monkeypatch.setattr(fleet_preflight, "Client", FakeClient)
    monkeypatch.setattr(sys, "argv", ["fleet-preflight", "--url", URL, "--project", "skybuild",
                                      "--token-file", str(token_file), "--principal", "wonko-worker",
                                      "--task-id", "TASK-1", "--expected-task-revision", "8"])
    assert fleet_preflight.main() == 2
    output = capsys.readouterr().out
    assert output == '{"ready": false, "reason": "Private API or worker scope check failed"}\n'
    assert TOKEN not in output
    assert "private task description" not in output
