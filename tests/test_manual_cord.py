"""Manual Cord relay persists only checked assignments and exact owned results."""

import json
import subprocess

import pytest

import skybuild.manual_cord as manual_cord
from skybuild.manual_cord import ManualCordError, receive_assignment, send_result
from test_manual_assignment import pinned as legacy_pinned  # noqa: F401


@pytest.fixture
def pinned(legacy_pinned):
    repo, envelope = legacy_pinned
    return repo, envelope | {"schema": "manual-work-v2", "task_status": "ready", "task_revision": 2}


class FakeClient:
    def __init__(self, messages=None):
        self.messages = messages or []
        self.actions = []
        self.sent = []
        self.task = {"task_id": "SKYBUILD-TASK-CUTOVER", "status": "ready", "revision": 2}

    def inbox(self, project, *, limit, offset):
        assert (project, limit, offset) == ("skybuild", 100, 0)
        return self.messages

    def get_task(self, project, task_id):
        return self.task | {"task_id": task_id}

    def message_action(self, project, message_id, action, *, idempotency_key):
        self.actions.append((project, message_id, action, idempotency_key))
        return {"message_id": message_id}

    def send_message(self, project, message, *, idempotency_key):
        self.sent.append((project, message, idempotency_key))
        return {"message_id": "result-1"}


def message(envelope, **changes):
    return {"message_id": "assignment-1", "sender": "jeltz", "recipient": "wonko",
            "category": "manual-work", "body": json.dumps(envelope)} | changes


def test_receive_checks_sender_and_committed_brief_before_receipt(pinned, tmp_path):
    repo, envelope = pinned
    client = FakeClient([message(envelope)])
    destination = tmp_path / "assignment.json"
    result = receive_assignment(client, "skybuild", repo, worker="wonko", dispatcher="jeltz",
                                message_id="assignment-1", destination=destination)
    assert result["verified"] is True
    assert result["authority"] == "api"
    assert result["task_revision"] == 2
    assert json.loads(destination.read_text()) == envelope
    assert destination.stat().st_mode & 0o777 == 0o600
    assert len(client.actions) == 1
    assert receive_assignment(client, "skybuild", repo, worker="wonko", dispatcher="jeltz",
                              message_id="assignment-1", destination=destination) == result


@pytest.mark.parametrize("change", [{"revision": 3}, {"revision": True}, {"status": "blocked"}])
def test_stale_task_never_saved_or_receipted(pinned, tmp_path, change):
    repo, envelope = pinned
    client = FakeClient([message(envelope)])
    client.task.update(change)
    destination = tmp_path / "assignment.json"
    with pytest.raises(ManualCordError, match="Task changed"):
        receive_assignment(client, "skybuild", repo, worker="wonko", dispatcher="jeltz",
                           message_id="assignment-1", destination=destination)
    assert not destination.exists()
    assert client.actions == []


def test_legacy_assignment_requires_redispatch(legacy_pinned, tmp_path):
    repo, envelope = legacy_pinned
    client = FakeClient([message(envelope)])
    destination = tmp_path / "assignment.json"
    with pytest.raises(ManualCordError, match="API-bound redispatch"):
        receive_assignment(client, "skybuild", repo, worker="wonko", dispatcher="jeltz",
                           message_id="assignment-1", destination=destination)
    assert not destination.exists()
    assert client.actions == []


@pytest.mark.parametrize("first_failure_at", [1, 2])
def test_receive_retry_syncs_existing_snapshot_before_receipt(pinned, tmp_path, monkeypatch,
                                                                 first_failure_at):
    repo, envelope = pinned
    client = FakeClient([message(envelope)])
    destination = tmp_path / "assignment.json"
    actual_fsync = manual_cord.os.fsync
    calls = []

    def fail_first_sync(descriptor):
        calls.append(descriptor)
        if len(calls) == first_failure_at:
            raise OSError("injected fsync failure")
        actual_fsync(descriptor)

    monkeypatch.setattr(manual_cord.os, "fsync", fail_first_sync)
    with pytest.raises(OSError, match="injected fsync failure"):
        receive_assignment(client, "skybuild", repo, worker="wonko", dispatcher="jeltz",
                           message_id="assignment-1", destination=destination)
    assert destination.exists()
    assert client.actions == []
    assert len(calls) == first_failure_at

    def fail_retry_sync(descriptor):
        raise OSError("injected fsync failure")

    monkeypatch.setattr(manual_cord.os, "fsync", fail_retry_sync)
    with pytest.raises(OSError, match="injected fsync failure"):
        receive_assignment(client, "skybuild", repo, worker="wonko", dispatcher="jeltz",
                           message_id="assignment-1", destination=destination)
    assert client.actions == []

    synced = []

    def record_sync(descriptor):
        synced.append(descriptor)
        actual_fsync(descriptor)

    monkeypatch.setattr(manual_cord.os, "fsync", record_sync)
    receive_assignment(client, "skybuild", repo, worker="wonko", dispatcher="jeltz",
                       message_id="assignment-1", destination=destination)
    assert len(synced) == 2
    assert len(client.actions) == 1


@pytest.mark.parametrize("change", [{"sender": "impostor"}, {"recipient": "another"},
                                     {"category": "misc"}, {"body": "not json"}])
def test_receive_refuses_untrusted_message_without_receipt(pinned, tmp_path, change):
    repo, envelope = pinned
    client = FakeClient([message(envelope, **change)])
    destination = tmp_path / "assignment.json"
    with pytest.raises(ManualCordError):
        receive_assignment(client, "skybuild", repo, worker="wonko", dispatcher="jeltz",
                           message_id="assignment-1", destination=destination)
    assert not destination.exists()
    assert client.actions == []


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def test_result_sends_exact_clean_head_and_owned_paths(pinned, tmp_path):
    repo, envelope = pinned
    worktree = tmp_path / "worker"
    git(repo, "worktree", "add", "-b", envelope["branch"], str(worktree), envelope["base_sha"])
    owned = worktree / "src/skybuild/client.py"
    owned.parent.mkdir(parents=True)
    owned.write_text("client change\n")
    git(worktree, "add", "src/skybuild/client.py")
    git(worktree, "commit", "-m", "Pilot change")
    head = git(worktree, "rev-parse", "HEAD")
    result = {"schema": "manual-work-v1", "assignment_id": envelope["assignment_id"],
              "phase": "ready-for-review", "branch": envelope["branch"], "head_sha": head,
              "checks": ["focused tests passed"], "changed_paths": ["src/skybuild/client.py"],
              "risks": [], "next_action": "Independent review"}
    client = FakeClient()
    sent = send_result(client, "skybuild", repo, worktree, worker="wonko",
                       assignment=envelope, result=result)
    assert sent == {"assignment_id": envelope["assignment_id"], "head_sha": head,
                    "message_id": "result-1", "sent": True}
    assert json.loads(client.sent[0][1]["body"]) == result
    assert client.sent[0][1]["recipient"] == "jeltz"
    assert client.sent[0][1]["category"] == "manual-work"
    owned.write_text("uncommitted\n")
    with pytest.raises(ManualCordError, match="clean"):
        send_result(client, "skybuild", repo, worktree, worker="wonko",
                    assignment=envelope, result=result)
    assert len(client.sent) == 1


def test_owner_relay_preserves_result_key_and_names_original_worker(pinned):
    _, envelope = pinned
    result = {"schema": "manual-work-v1", "assignment_id": envelope["assignment_id"],
              "phase": "ready-for-review", "branch": envelope["branch"], "head_sha": "a" * 40,
              "checks": ["focused tests passed"], "changed_paths": ["src/skybuild/client.py"],
              "risks": [], "next_action": "Independent review"}
    worker_message, worker_key = manual_cord.result_message(envelope, result)
    relay_message, relay_key = manual_cord.result_message(envelope, result, relay_worker="wonko")
    assert relay_key == worker_key
    assert relay_message["body"] == worker_message["body"]
    assert relay_message["subject"] == "Manual work result (trusted owner relay for wonko)"
    assert relay_message["recipient"] == worker_message["recipient"]


def test_result_refuses_changed_path_outside_scope(pinned, tmp_path):
    repo, envelope = pinned
    worktree = tmp_path / "worker"
    git(repo, "worktree", "add", "-b", envelope["branch"], str(worktree), envelope["base_sha"])
    (worktree / "outside.txt").write_text("outside\n")
    git(worktree, "add", "outside.txt")
    git(worktree, "commit", "-m", "Outside")
    result = {"schema": "manual-work-v1", "assignment_id": envelope["assignment_id"],
              "phase": "ready-for-review", "branch": envelope["branch"], "head_sha": git(worktree, "rev-parse", "HEAD"),
              "checks": [], "changed_paths": ["outside.txt"], "risks": [], "next_action": "Review"}
    client = FakeClient()
    with pytest.raises(ManualCordError, match="scope"):
        send_result(client, "skybuild", repo, worktree, worker="wonko",
                    assignment=envelope, result=result)
    assert client.sent == []


def test_result_refuses_rename_from_outside_scope(pinned, tmp_path):
    repo, envelope = pinned
    (repo / "outside.txt").write_text("outside\n")
    git(repo, "add", "outside.txt")
    git(repo, "commit", "-m", "Add outside source")
    envelope["base_sha"] = git(repo, "rev-parse", "HEAD")
    worktree = tmp_path / "worker"
    git(repo, "worktree", "add", "-b", envelope["branch"], str(worktree), envelope["base_sha"])
    owned = worktree / "src/skybuild/client.py"
    owned.parent.mkdir(parents=True)
    git(worktree, "mv", "outside.txt", "src/skybuild/client.py")
    git(worktree, "commit", "-m", "Rename outside source")
    result = {"schema": "manual-work-v1", "assignment_id": envelope["assignment_id"],
              "phase": "ready-for-review", "branch": envelope["branch"], "head_sha": git(worktree, "rev-parse", "HEAD"),
              "checks": [], "changed_paths": ["src/skybuild/client.py"], "risks": [], "next_action": "Review"}
    client = FakeClient()
    with pytest.raises(ManualCordError, match="scope"):
        send_result(client, "skybuild", repo, worktree, worker="wonko",
                    assignment=envelope, result=result)
    assert client.sent == []

def test_legacy_receive_adapter_binds_real_producer_to_cpu_bridge(pinned, tmp_path):
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from skybuild.cpu_worker_bridge import CPUWorkerBridgeError, _read_assignment

    repo, envelope = pinned
    token = {"project_id": "skybuild", "task_id": envelope["task_id"],
             "place": "ready", "pending_action": None, "superseded": False}

    class FencedClient(FakeClient):
        def get_task(self, project, task_id):
            return super().get_task(project, task_id) | {
                "metadata": {"_skybuild_workflow": {"petri": {"schema_version": 1, "token": token}}}}

        def claim_task(self, project, task_id, **kwargs):
            assert task_id == envelope["task_id"]
            assert kwargs["expected_revision"] == 2
            return {"holder": "wonko", "held": True, "fence": 1, "task_revision": 3,
                    "lease_until": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()}

        def task_workflow(self, project, task_id):
            return {"token": token | {"place": "working", "attempt_id": "attempt-one",
                                      "claim_fence": 1, "revision": 3}}

    state = tmp_path / "cpu-assignment"
    state.mkdir(mode=0o700)
    received = receive_assignment(FencedClient([message(envelope)]), "skybuild", repo,
        worker="wonko", dispatcher="jeltz", message_id="assignment-1",
        destination=state / "assignment.json", expected_envelope=envelope)
    preclaim = state / "preclaim.json"
    from skybuild.auto_patch_controller import _bind_legacy_preclaim
    assert "task_id" not in received
    bound = _bind_legacy_preclaim(received, state / "assignment.json",
        expected_envelope=envelope, project="skybuild")
    assert "task_id" not in received
    preclaim.write_text(json.dumps(bound))
    preclaim.chmod(0o600)
    plan = SimpleNamespace(assignment_dir=state, checkout=repo,
                           worker_id="wonko", dispatcher_id="jeltz")
    assignment, claim, workflow, digest = _read_assignment(plan)
    assert claim["task_id"] == assignment["task_id"] == envelope["task_id"]
    assert claim["attempt_id"] == workflow["token"]["attempt_id"] == "attempt-one"
    assert claim["claim_fence"] == workflow["token"]["claim_fence"] == 1
    assert len(digest) == 64
    for changed in ({k: v for k, v in bound.items() if k != "task_id"},
                    bound | {"task_id": "SKYBUILD-OTHER"}):
        preclaim.write_text(json.dumps(changed))
        with pytest.raises(CPUWorkerBridgeError, match="fenced preclaim identity"):
            _read_assignment(plan)

    from skybuild.auto_patch_controller import AutoControllerError
    for changed in (received | {"attempt_id": "other"}, received | {"claim_fence": 2},
                    received | {"task_id": "SKYBUILD-OTHER"}):
        with pytest.raises(AutoControllerError, match="exact saved claim"):
            _bind_legacy_preclaim(changed, state / "assignment.json",
                expected_envelope=envelope, project="skybuild")
    with pytest.raises(AutoControllerError, match="exact saved claim"):
        _bind_legacy_preclaim(received, state / "assignment.json",
            expected_envelope=envelope, project="other-project")
