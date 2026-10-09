"""Manual Cord relay persists only checked assignments and exact owned results."""

import json
import subprocess

import pytest

from skybuild.manual_cord import ManualCordError, receive_assignment, send_result
from test_manual_assignment import pinned  # noqa: F401


class FakeClient:
    def __init__(self, messages=None):
        self.messages = messages or []
        self.actions = []
        self.sent = []

    def inbox(self, project, *, limit, offset):
        assert (project, limit, offset) == ("skybuild", 100, 0)
        return self.messages

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
    assert json.loads(destination.read_text()) == envelope
    assert destination.stat().st_mode & 0o777 == 0o600
    assert len(client.actions) == 1
    assert receive_assignment(client, "skybuild", repo, worker="wonko", dispatcher="jeltz",
                              message_id="assignment-1", destination=destination) == result


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
