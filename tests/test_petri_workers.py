"""Worker claims and submissions preserve wire bodies and durable identities."""
from copy import deepcopy
import json

import pytest

from skybuild.client import ClientError
from skybuild.manual_cord import (
    ManualCordError, _claim_assignment, _current_assignment, _submit_result, _workflow_path,
    renew_assignment, receive_assignment, send_result,
)
from skybuild.manual_dispatch import DispatchError, _require_dispatchable_place
from skybuild.workflow import Place, TaskToken
from test_manual_cord import pinned, git, message  # noqa: F401
from test_manual_assignment import pinned as legacy_pinned  # noqa: F401


@pytest.fixture
def worker(tmp_path):
    assignment = {"assignment_id": "assignment-1", "task_id": "TASK-1", "task_revision": 2,
                  "branch": "task/work", "base_sha": "b" * 40}
    token = TaskToken("project", "TASK-1", place=Place.READY, revision=2,
                      source_head="b" * 40, target_base="b" * 40,
                      definition_revision=1, input_generation=3, policy_version="policy").to_dict()
    class FakeClient:
        def __init__(self):
            self.token = deepcopy(token)
            self.claims = []
            self.submissions = []
            self.lost_claim = False
            self.lost_submit = False
            self.claim_result = None
        def get_task(self, project, task_id):
            return {"task_id": task_id, "revision": self.token["revision"],
                    "metadata": {"_skybuild_workflow": {"petri": {"schema_version": 1, "token": self.token}}}}
        def claim_task(self, project, task_id, **kwargs):
            self.claims.append(kwargs)
            if self.claim_result is None:
                self.token.update(place="working", revision=3, attempt_id="attempt-1", claim_fence=1)
                self.claim_result = {"holder": "worker", "held": True, "fence": 1, "task_revision": 3,
                                     "lease_until": "2030-01-01T00:00:00+00:00"}
            if self.lost_claim:
                self.lost_claim = False
                raise ClientError("unavailable", "Lost claim reply")
            return self.claim_result
        def task_workflow(self, project, task_id):
            return {"token": deepcopy(self.token)}
        def workflow_transition(self, project, task_id, event, body, **kwargs):
            self.submissions.append((event, body, kwargs))
            self.token.update(place="validating", revision=4)
            if self.lost_submit:
                self.lost_submit = False
                raise ClientError("unavailable", "Lost submit reply")
            return {"token": self.token}
        def request(self, method, path, **kwargs):
            self.renewal = (method, path, kwargs)
            return self.claim_result
    client = FakeClient()
    destination = tmp_path / "assignment.json"
    return client, assignment, destination


def claim(worker):
    client, assignment, destination = worker
    return _claim_assignment(client, "project", assignment, client.get_task("project", "TASK-1"), destination, "worker")


def report():
    return {"phase": "ready-for-review", "head_sha": "a" * 40}


def test_lost_claim_reply_replays_same_request_and_private_snapshot(worker):
    client, assignment, destination = worker
    client.lost_claim = True
    with pytest.raises(ClientError):
        claim(worker)
    assert not _workflow_path(destination).exists()
    state = claim(worker)
    assert client.claims[0] == client.claims[1]
    assert state["token"]["attempt_id"] == "attempt-1"
    assert _workflow_path(destination).stat().st_mode & 0o777 == 0o600
    assert claim(worker) == state
    assert assignment == worker[1]


@pytest.mark.parametrize("change", [{"place": "working"}, {"pending_action": "hold"}, {"superseded": True}])
def test_assignment_cannot_claim_unpermitted_work(worker, change):
    client, _, _ = worker
    client.token.update(change)
    with pytest.raises(ManualCordError):
        claim(worker)
    assert client.claims == []


def test_changed_assignment_cannot_reuse_claim_identity(worker):
    claim(worker)
    client, assignment, _ = worker
    assignment["branch"] = "task/different"
    with pytest.raises(ManualCordError):
        claim(worker)
    assert len(client.claims) == 1


def test_expired_claim_replay_never_restores_assignment_permission(worker):
    claim(worker)
    client, _, _ = worker
    client.claim_result["lease_until"] = "2020-01-01T00:00:00+00:00"
    with pytest.raises(ManualCordError, match="lease"):
        claim(worker)


def test_lost_submission_reply_replays_exact_proposed_output(worker):
    claim(worker)
    client, assignment, destination = worker
    path = _workflow_path(destination)
    client.lost_submit = True
    with pytest.raises(ClientError):
        _submit_result(client, "project", assignment, report(), "worker", path, "result-key")
    _submit_result(client, "project", assignment, report(), "worker", path, "result-key")
    assert client.submissions[0] == client.submissions[1]
    _, body, kwargs = client.submissions[0]
    assert body["source_branch"] == "refs/heads/task/work"
    assert body["source_head"] == "a" * 40 and body["attempt_id"] == "attempt-1"
    assert kwargs["expected_revision"] == 3
    assert path.with_name(path.name + ".submit").stat().st_mode & 0o777 == 0o600
    assert set(body) == {"source_head", "target_base", "source_branch", "attempt_id", "claim_fence",
                         "input_generation", "definition_revision", "policy_version"}
    with pytest.raises(ManualCordError, match="differs"):
        _submit_result(client, "project", assignment, report() | {"head_sha": "c" * 40}, "worker", path, "different-key")


@pytest.mark.parametrize("change", [{"claim_fence": 2}, {"attempt_id": "other"}, {"input_generation": 4},
    {"definition_revision": 2}, {"policy_version": "new"}, {"pending_action": "defer"}, {"target_base": "c" * 40}])
def test_stale_result_is_not_submitted(worker, change):
    claim(worker)
    client, assignment, destination = worker
    client.token.update(change)
    with pytest.raises(ManualCordError):
        _submit_result(client, "project", assignment, report(), "worker", _workflow_path(destination), "key")
    assert client.submissions == []


def test_renewal_uses_saved_fence_and_current_revision(worker):
    claim(worker)
    client, assignment, destination = worker
    renewed = renew_assignment(client, "project", assignment, "worker", _workflow_path(destination))
    assert renewed["renewed"] is True and renewed["claim_fence"] == 1
    method, path, kwargs = client.renewal
    assert method == "POST" and path.endswith("tasks/TASK-1/claim/renew")
    assert kwargs["body"] == {"fence": 1, "lease_seconds": 300} and kwargs["revision"] == 3
    client.token["claim_fence"] = 2
    with pytest.raises(ManualCordError):
        renew_assignment(client, "project", assignment, "worker", _workflow_path(destination))


@pytest.mark.parametrize("place,pending", [("working", None), ("ready", "hold"), ("integrating", None)])
def test_dispatch_cannot_send_pending_or_nonready_petritask(place, pending):
    task = {"metadata": {"_skybuild_workflow": {"petri": {"schema_version": 1,
             "token": {"place": place, "pending_action": pending}}}}}
    with pytest.raises(DispatchError):
        _require_dispatchable_place(task)


def test_real_worker_receive_and_result_paths_preserve_cord_envelopes(worker, pinned, monkeypatch):
    client, _, destination = worker
    repo, assignment = pinned
    client.token.update(project_id="skybuild", task_id=assignment["task_id"], target_base=assignment["base_sha"])
    client.inbox = lambda *_args, **_kwargs: [message(assignment)]
    receipts, sent = [], []
    client.message_action = lambda *args, **kwargs: receipts.append((args, kwargs))
    original_claim = client.claim_task
    def bound_claim(*args, **kwargs):
        claim = original_claim(*args, **kwargs)
        claim["holder"] = "wonko"
        return claim
    client.claim_task = bound_claim
    client.send_message = lambda *args, **kwargs: sent.append((args, kwargs)) or {"message_id": "result-1"}
    accepted = receive_assignment(client, "skybuild", repo, worker="wonko", dispatcher="jeltz",
                                  message_id="assignment-1", destination=destination)
    assert accepted["place"] == "working" and receipts
    assert json.loads(destination.read_text()) == assignment
    worktree = destination.parent / "work"
    git(repo, "worktree", "add", "-b", assignment["branch"], str(worktree), assignment["base_sha"])
    owned = worktree / "src/skybuild/client.py"
    owned.parent.mkdir(parents=True)
    owned.write_text("proposed work\n")
    git(worktree, "add", ".")
    git(worktree, "commit", "-m", "Proposed work")
    report = {"schema": "manual-work-v1", "assignment_id": assignment["assignment_id"],
              "phase": "ready-for-review", "branch": assignment["branch"],
              "head_sha": git(worktree, "rev-parse", "HEAD"), "checks": ["unit tests passed"],
              "changed_paths": ["src/skybuild/client.py"], "risks": [], "next_action": "Independent review"}
    output = send_result(client, "skybuild", repo, worktree, worker="wonko", assignment=assignment,
                         result=report, workflow_state=_workflow_path(destination))
    assert output["sent"] is True and client.token["place"] == "validating"
    assert json.loads(sent[0][0][1]["body"]) == report
    assert client.submissions[0][1]["source_head"] == report["head_sha"]
