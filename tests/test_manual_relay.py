"""Exercise dispatcher and worker contracts across the authenticated Cord API."""

import json
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from skybuild import manual_dispatch
from skybuild.api import create_app
from skybuild.client import Client
from skybuild.manual_cord import receive_assignment, send_result
from skybuild.store import Store
from test_manual_assignment import pinned  # noqa: F401
from test_manual_dispatch import FakeClient, _git
from test_store import seed_api_authority


@pytest.fixture(params=["wonko", "wowbagger"])
def relay_assignment(pinned, request, monkeypatch):
    repo, envelope = pinned
    path = envelope["brief_path"]
    brief = json.loads((repo / path).read_text())
    brief.update(task_id="SKYBUILD-TASK-CUTOVER", worker=request.param, dispatcher="pilot_dispatcher")
    (repo / path).write_text(json.dumps(brief))
    _git(repo, "add", path)
    _git(repo, "commit", "-qm", "Bind pilot identities")
    base = _git(repo, "rev-parse", "HEAD")
    monkeypatch.setattr(manual_dispatch, "_published_head",
                        lambda _repo, base_ref: base if base_ref == "refs/heads/dev-003" else None)
    return repo, path, brief


def dispatch_assignment(relay_assignment, tmp_path, client_factory, project="skybuild", token_value="x" * 32):
    repo, path, brief = relay_assignment
    token = tmp_path / "dispatcher-token"
    token.write_text(token_value)
    token.chmod(0o600)
    return manual_dispatch.dispatch(
        repo, path, worker=brief["worker"], dispatcher=brief["dispatcher"],
        principal=brief["dispatcher"], project=project, url="https://controller.ts.net",
        token_file=token, state_dir=tmp_path / "dispatch-state",
        resolve=lambda _host: ["100.101.102.103"], client_factory=client_factory)


def test_dispatcher_message_is_accepted_by_worker(relay_assignment, tmp_path):
    repo, _, brief = relay_assignment
    sender = FakeClient()
    sender.task["task_id"] = brief["task_id"]
    sent = dispatch_assignment(relay_assignment, tmp_path, lambda *_args, **_kwargs: sender)
    body = sender.calls[0][1]
    from test_manual_cord import FakeClient as WorkerClient
    receiver = WorkerClient([{**body, "sender": brief["dispatcher"], "message_id": sent["message_id"]}])
    destination = tmp_path / "received.json"
    received = receive_assignment(receiver, "skybuild", repo, worker=brief["worker"],
                                  dispatcher=brief["dispatcher"], message_id=sent["message_id"],
                                  destination=destination)
    assert received["verified"] is True
    assert json.loads(destination.read_text())["worker"] == brief["worker"]
    assert len(receiver.actions) == 1


def test_authenticated_assignment_and_result_roundtrip(relay_assignment, restricted_database, tmp_path):
    admin_dsn, runtime_dsn, database, _ = restricted_database
    repo, _, brief = relay_assignment
    project = "relay-" + uuid4().hex
    dispatcher, worker = brief["dispatcher"], brief["worker"]
    admin = Store(admin_dsn, database)
    seed_api_authority(admin, project)
    scopes = ["tasks:read", "cord:send", "cord:read", "cord:handle"]
    dispatcher_token, worker_token = uuid4().hex + uuid4().hex, uuid4().hex + uuid4().hex
    admin.provision_principal(dispatcher, dispatcher_token, grants={project: scopes})
    admin.provision_principal(worker, worker_token, grants={project: scopes + ["tasks:claim", "tasks:write"]})
    owner_token = uuid4().hex + uuid4().hex
    admin.provision_principal("owner-" + project, owner_token, is_admin=True)
    owner = admin.authenticate(owner_token)
    task = admin.create_task(owner, project, {
        "task_id": brief["task_id"], "title": "Relay coding task", "description": "Bounded relay test",
        "acceptance_criteria": ["Worker receives an API-bound assignment and reports a result"]}, "create")
    assert task["status"] == "ready"
    assert Store.workflow_token(task).place.value == "ready"
    with TestClient(create_app(Store(runtime_dsn, database))) as api:
        def transport(request):
            response = api.request(request.method, str(request.url),
                                   headers=dict(request.headers), content=request.content)
            return httpx.Response(response.status_code, headers=response.headers, content=response.content)

        def client_factory(url, token, **kwargs):
            return Client(url, token, transport=httpx.MockTransport(transport), **kwargs)

        sent = dispatch_assignment(relay_assignment, tmp_path, client_factory, project, dispatcher_token)
        destination = tmp_path / "received.json"
        with client_factory("https://controller.ts.net", worker_token) as receiver:
            received = receive_assignment(receiver, project, repo, worker=worker, dispatcher=dispatcher,
                                          message_id=sent["message_id"], destination=destination)
            assignment = json.loads(destination.read_text())
            assert assignment["schema"] == "manual-work-v2"
            assert assignment["task_revision"] == task["revision"]
            assert assignment["task_status"] == "ready"
            assert received["place"] == "working"
            claimed = Store.workflow_token(admin.get_task(owner, project, task["task_id"]))
            assert claimed.attempt_id == received["attempt_id"]
            assert claimed.claim_fence == received["claim_fence"]
            worktree = tmp_path / "worker"
            _git(repo, "worktree", "add", "-b", assignment["branch"], str(worktree), assignment["base_sha"])
            result = {"schema": "manual-work-v1", "assignment_id": assignment["assignment_id"],
                      "phase": "blocked", "branch": assignment["branch"], "head_sha": assignment["base_sha"],
                      "checks": [], "changed_paths": [], "risks": [], "next_action": "Reconcile blocker"}
            report = send_result(receiver, project, repo, worktree, worker=worker,
                                 assignment=assignment, result=result,
                                 workflow_state=destination.with_name(destination.name + ".workflow.json"))
        with client_factory("https://controller.ts.net", dispatcher_token) as sender:
            inbox = sender.inbox(project)
            assert len(inbox) == 1
            message = inbox[0]
            assert (message["sender"], message["recipient"], message["category"]) == (worker, dispatcher, "manual-work")
            assert json.loads(message["body"]) == result
            sender.message_action(project, report["message_id"], "handle")
            assert sender.inbox(project) == []
