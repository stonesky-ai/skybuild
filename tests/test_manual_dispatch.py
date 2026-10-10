"""Pinned manual dispatch and crash-safe Cord retry tests."""

import hashlib
import json
import subprocess

import pytest

from skybuild import manual_dispatch


def _brief():
    return {"schema": "manual-work-brief-v1", "assignment_id": "MWP-test-1",
            "task_id": "SKYBUILD-TASK-CUTOVER", "worker": "wonko", "dispatcher": "pilot_dispatcher",
            "branch": "task/manual-test", "owned_paths": ["src/skybuild/new_module.py"],
            "checks": ["Run focused tests"], "model_limit": "One existing subscription worker",
            "next_action": "Implement the bounded change."}


def _git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.DEVNULL).decode().strip()


def test_build_envelope_from_committed_bytes(tmp_path, monkeypatch):
    repo = tmp_path / "checkout"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "SkyBuild Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "remote", "add", "origin", "https://github.com/stonesky-ai/skybuild.git")
    (repo / "docs/design/assignments").mkdir(parents=True)
    (repo / "docs/design/architecture.md").write_text("Architecture\n")
    path = "docs/design/assignments/test.json"
    raw = (json.dumps(_brief(), indent=2) + "\n").encode()
    (repo / path).write_bytes(raw)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "test brief")
    base = _git(repo, "rev-parse", "HEAD")
    monkeypatch.setattr(manual_dispatch, "_published_head", lambda _repo, _base_ref: base)
    envelope = manual_dispatch.build_envelope(repo, path, worker="wonko", dispatcher="pilot_dispatcher")
    assert envelope["base_sha"] == base
    assert envelope["brief_sha256"] == hashlib.sha256(raw).hexdigest()
    assert envelope["schema"] == "manual-work-v1"
    with pytest.raises(manual_dispatch.DispatchError, match="another worker"):
        manual_dispatch.build_envelope(repo, path, worker="wowbaggers", dispatcher="pilot_dispatcher")


class FakeClient:
    def __init__(self):
        self.calls = []
        self.fail_once = False
        self.task = {"task_id": "SKYBUILD-TASK-CUTOVER", "status": "ready", "revision": 2}
        self.task_reads = 0
        self.grants = {"skybuild": ["cord:send", "cord:read", "cord:handle", "tasks:read"]}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def request(self, method, path):
        assert (method, path) == ("GET", "health/ready")
        return {"status": "ready"}

    def whoami(self):
        return {"principal_id": "pilot_dispatcher", "is_admin": False, "grants": self.grants}

    def get_task(self, project, task_id):
        assert (project, task_id) == ("skybuild", "SKYBUILD-TASK-CUTOVER")
        self.task_reads += 1
        return self.task

    def send_message(self, project, body, *, idempotency_key):
        self.calls.append((project, body, idempotency_key))
        if self.fail_once:
            self.fail_once = False
            raise manual_dispatch.ClientError("unavailable", "Lost reply")
        return {"message_id": "11111111-1111-1111-1111-111111111111"}


def _dispatch(tmp_path, monkeypatch, client):
    repo = tmp_path / "checkout"
    repo.mkdir()
    state = tmp_path / "state"
    token = tmp_path / "token"
    token.write_text("x" * 32)
    token.chmod(0o600)
    envelope = {"schema": "manual-work-v1", "assignment_id": "MWP-test-1",
                "task_id": "SKYBUILD-TASK-CUTOVER", "worker": "wonko", "dispatcher": "pilot_dispatcher",
                "base_sha": "a" * 40, "brief_path": "docs/design/assignments/test.json",
                "brief_sha256": "b" * 64, "branch": "task/manual-test",
                "owned_paths": ["src/skybuild/new_module.py"], "checks": ["Run focused tests"],
                "model_limit": "One existing subscription worker"}
    monkeypatch.setattr(manual_dispatch, "build_envelope", lambda *_args, **_kwargs: envelope)
    monkeypatch.setattr(manual_dispatch, "verify_assignment", lambda *_args, **_kwargs: {"verified": True})
    monkeypatch.setattr(manual_dispatch, "_published_head", lambda _repo, _base_ref: "a" * 40)
    kwargs = {"worker": "wonko", "dispatcher": "pilot_dispatcher", "project": "skybuild",
              "principal": "pilot_dispatcher", "url": "https://controller.ts.net",
              "token_file": token, "state_dir": state,
              "resolve": lambda _host: ["100.101.102.103"],
              "client_factory": lambda *_args, **_kwargs: client}
    return repo, state, envelope, kwargs


def test_dispatch_pins_ca_and_rejects_changed_or_removed_trust(tmp_path, monkeypatch):
    client = FakeClient()
    client.fail_once = True
    repo, state, _, kwargs = _dispatch(tmp_path, monkeypatch, client)
    ca = tmp_path / "installation-ca.pem"
    ca.write_bytes(b"original test trust material")
    captured = []
    def factory(*args, **options):
        captured.append(options)
        return client
    kwargs.update(ca_file=ca, client_factory=factory)
    with pytest.raises(manual_dispatch.DispatchError, match="request failed"):
        manual_dispatch.dispatch(repo, "docs/design/assignments/test.json", **kwargs)
    path = next(state.glob("*.json"))
    before = path.read_bytes()
    fingerprint = hashlib.sha256(ca.read_bytes()).hexdigest()
    assert json.loads(before)["ca_sha256"] == fingerprint
    assert captured[0]["expected_ca_sha256"] == fingerprint
    ca.write_bytes(b"changed test trust material")
    with pytest.raises(manual_dispatch.DispatchError, match="durable intent"):
        manual_dispatch.dispatch(repo, "docs/design/assignments/test.json", **kwargs)
    kwargs["ca_file"] = None
    with pytest.raises(manual_dispatch.DispatchError, match="durable intent"):
        manual_dispatch.dispatch(repo, "docs/design/assignments/test.json", **kwargs)
    assert path.read_bytes() == before
    assert len(captured) == 1
    ca.write_bytes(b"original test trust material")
    kwargs["ca_file"] = ca
    result = manual_dispatch.dispatch(repo, "docs/design/assignments/test.json", **kwargs)
    assert result["status"] == "sent"
    assert json.loads(path.read_text())["ca_sha256"] == fingerprint
    assert client.calls[0] == client.calls[1]


def test_existing_system_trust_intent_cannot_silently_add_ca(tmp_path, monkeypatch):
    client = FakeClient()
    client.fail_once = True
    repo, state, _, kwargs = _dispatch(tmp_path, monkeypatch, client)
    with pytest.raises(manual_dispatch.DispatchError):
        manual_dispatch.dispatch(repo, "docs/design/assignments/test.json", **kwargs)
    path = next(state.glob("*.json"))
    before = path.read_bytes()
    assert "ca_sha256" not in json.loads(before)
    ca = tmp_path / "new-ca.pem"
    ca.write_bytes(b"new test trust material")
    with pytest.raises(manual_dispatch.DispatchError, match="durable intent"):
        manual_dispatch.dispatch(repo, "docs/design/assignments/test.json", ca_file=ca, **kwargs)
    assert path.read_bytes() == before
    # Existing intents retain their old endpoint binding and idempotency key.
    assert manual_dispatch.dispatch(repo, "docs/design/assignments/test.json", **kwargs)["status"] == "sent"


def test_retry_uses_same_intent_and_key(tmp_path, monkeypatch):
    client = FakeClient()
    client.fail_once = True
    repo, state_dir, _, kwargs = _dispatch(tmp_path, monkeypatch, client)
    with pytest.raises(manual_dispatch.DispatchError, match="unavailable"):
        manual_dispatch.dispatch(repo, "docs/design/assignments/test.json", **kwargs)
    prepared = list(state_dir.glob("*.json"))
    assert len(prepared) == 1
    assert json.loads(prepared[0].read_text())["status"] == "sending"
    sent_envelope = json.loads(client.calls[0][1]["body"])
    assert (sent_envelope["schema"], sent_envelope["task_status"], sent_envelope["task_revision"]) == (
        "manual-work-v2", "ready", 2)
    client.task = {"task_id": "SKYBUILD-TASK-CUTOVER", "status": "blocked", "revision": 3}
    monkeypatch.setattr(manual_dispatch, "_published_head", lambda _repo, _base_ref: "c" * 40)
    sent = manual_dispatch.dispatch(repo, "docs/design/assignments/test.json", **kwargs)
    assert sent["status"] == "sent"
    assert sent["mode"] == "pinned_retry"
    assert len(client.calls) == 2
    assert client.calls[0] == client.calls[1]
    assert client.task_reads == 1
    assert json.loads(prepared[0].read_text())["status"] == "sent"
    manual_dispatch.dispatch(repo, "docs/design/assignments/test.json", **kwargs)
    assert len(client.calls) == 2


@pytest.mark.parametrize("change", [{"status": "blocked"}, {"status": "done"},
                                     {"revision": True}, {"revision": 0},
                                     {"task_id": "OTHER"}])
def test_ineligible_task_never_sends(tmp_path, monkeypatch, change):
    client = FakeClient()
    client.task.update(change)
    repo, state, _, kwargs = _dispatch(tmp_path, monkeypatch, client)
    with pytest.raises(manual_dispatch.DispatchError, match="not ready"):
        manual_dispatch.dispatch(repo, "docs/design/assignments/test.json", **kwargs)
    assert client.calls == []
    assert json.loads(next(state.glob("*.json")).read_text())["status"] == "prepared"


def test_prepared_bound_retry_rechecks_without_repinning(tmp_path, monkeypatch):
    client = FakeClient()
    repo, state, _, kwargs = _dispatch(tmp_path, monkeypatch, client)
    monkeypatch.setattr(manual_dispatch, "_published_head", lambda _repo, _base_ref: "c" * 40)
    with pytest.raises(manual_dispatch.DispatchError, match="head changed"):
        manual_dispatch.dispatch(repo, "docs/design/assignments/test.json", **kwargs)
    path = next(state.glob("*.json"))
    before = path.read_bytes()
    client.task["revision"] = 3
    with pytest.raises(manual_dispatch.DispatchError, match="Task changed"):
        manual_dispatch.dispatch(repo, "docs/design/assignments/test.json", **kwargs)
    assert path.read_bytes() == before
    assert client.calls == []


def test_changed_current_brief_cannot_replace_pinned_intent(tmp_path, monkeypatch):
    client = FakeClient()
    repo, _, envelope, kwargs = _dispatch(tmp_path, monkeypatch, client)
    manual_dispatch.dispatch(repo, "docs/design/assignments/test.json", **kwargs)
    envelope["owned_paths"] = ["src/skybuild/other.py"]
    result = manual_dispatch.dispatch(repo, "docs/design/assignments/test.json", **kwargs)
    assert result["mode"] == "already_sent"
    assert len(client.calls) == 1
    assert json.loads(client.calls[0][1]["body"])["owned_paths"] == ["src/skybuild/new_module.py"]


def test_private_url_and_scoped_principal_required(tmp_path, monkeypatch):
    client = FakeClient()
    repo, state_dir, _, kwargs = _dispatch(tmp_path, monkeypatch, client)
    with pytest.raises(manual_dispatch.DispatchError, match="private Tailscale"):
        manual_dispatch.dispatch(repo, "docs/design/assignments/test.json", **{**kwargs, "url": "https://public.example"})
    assert not state_dir.exists()
    client.grants = {"skybuild": ["cord:read"]}
    with pytest.raises(manual_dispatch.DispatchError, match="scoped dispatcher"):
        manual_dispatch.dispatch(repo, "docs/design/assignments/test.json", **kwargs)
    assert client.calls == []
    client.grants = {"skybuild": ["cord:send", "tasks:write"]}
    with pytest.raises(manual_dispatch.DispatchError, match="scoped dispatcher"):
        manual_dispatch.dispatch(repo, "docs/design/assignments/test.json", **kwargs)
    client.grants = {"skybuild": ["cord:send", "cord:send"]}
    with pytest.raises(manual_dispatch.DispatchError, match="scoped dispatcher"):
        manual_dispatch.dispatch(repo, "docs/design/assignments/test.json", **kwargs)


def test_retry_cannot_change_controller_port(tmp_path, monkeypatch):
    client = FakeClient()
    client.fail_once = True
    repo, _, _, kwargs = _dispatch(tmp_path, monkeypatch, client)
    with pytest.raises(manual_dispatch.DispatchError, match="unavailable"):
        manual_dispatch.dispatch(repo, "docs/design/assignments/test.json", **kwargs)
    with pytest.raises(manual_dispatch.DispatchError, match="durable intent"):
        manual_dispatch.dispatch(repo, "docs/design/assignments/test.json",
                                 **{**kwargs, "url": "https://controller.ts.net:444"})
    assert len(client.calls) == 1


def test_moved_development_head_blocks_send(tmp_path, monkeypatch):
    client = FakeClient()
    repo, state_dir, _, kwargs = _dispatch(tmp_path, monkeypatch, client)
    monkeypatch.setattr(manual_dispatch, "_published_head", lambda _repo, _base_ref: "c" * 40)
    with pytest.raises(manual_dispatch.DispatchError, match="head changed"):
        manual_dispatch.dispatch(repo, "docs/design/assignments/test.json", **kwargs)
    assert client.calls == []
    assert json.loads(next(state_dir.glob("*.json")).read_text())["status"] == "prepared"


def test_prepared_retry_reuses_pinned_body_after_head_moves(tmp_path, monkeypatch):
    client = FakeClient()
    client.grants = {"skybuild": ["cord:read"]}
    repo, state_dir, _, kwargs = _dispatch(tmp_path, monkeypatch, client)
    with pytest.raises(manual_dispatch.DispatchError, match="scoped dispatcher"):
        manual_dispatch.dispatch(repo, "docs/design/assignments/test.json", **kwargs)
    assert json.loads(next(state_dir.glob("*.json")).read_text())["status"] == "prepared"
    monkeypatch.setattr(manual_dispatch, "_published_head", lambda _repo, _base_ref: "c" * 40)
    client.grants = {"skybuild": ["cord:send", "cord:read", "cord:handle", "tasks:read"]}
    sent = manual_dispatch.dispatch(repo, "docs/design/assignments/test.json", **kwargs)
    assert sent["mode"] == "prepared_retry"
    assert json.loads(client.calls[0][1]["body"])["base_sha"] == "a" * 40


def test_large_valid_intent_can_be_retried(tmp_path, monkeypatch):
    client = FakeClient()
    client.fail_once = True
    repo, state_dir, envelope, kwargs = _dispatch(tmp_path, monkeypatch, client)
    envelope["owned_paths"] = [f"src/skybuild/feature_{number:04d}.py" for number in range(1020)]
    with pytest.raises(manual_dispatch.DispatchError, match="unavailable"):
        manual_dispatch.dispatch(repo, "docs/design/assignments/test.json", **kwargs)
    state_file = next(state_dir.glob("*.json"))
    assert state_file.stat().st_size > 65536
    result = manual_dispatch.dispatch(repo, "docs/design/assignments/test.json", **kwargs)
    assert result["mode"] == "pinned_retry"
    assert len(client.calls) == 2
    assert client.calls[0] == client.calls[1]


def test_dispatcher_must_match_authenticated_principal(tmp_path, monkeypatch):
    client = FakeClient()
    repo, state_dir, _, kwargs = _dispatch(tmp_path, monkeypatch, client)
    with pytest.raises(manual_dispatch.DispatchError, match="authenticated principal"):
        manual_dispatch.dispatch(repo, "docs/design/assignments/test.json",
                                 **{**kwargs, "dispatcher": "jeltz"})
    assert not state_dir.exists()
    assert client.calls == []


def test_published_head_uses_selected_development_ref(monkeypatch, tmp_path):
    calls = []
    def git(repo, *args):
        calls.append(args)
        if args[0] == "ls-remote":
            return ("a" * 40 + "\trefs/heads/dev-004\n").encode()
        return ("a" * 40 + "\n").encode()
    monkeypatch.setattr(manual_dispatch, "_git", git)
    assert manual_dispatch._published_head(tmp_path, "refs/heads/dev-004") == "a" * 40
    assert calls[-1][-1] == "refs/remotes/origin/dev-004"
    with pytest.raises(manual_dispatch.DispatchError, match="development branch"):
        manual_dispatch._published_head(tmp_path, "refs/heads/main")


def test_retry_cannot_change_frozen_development_ref(tmp_path, monkeypatch):
    client = FakeClient()
    client.fail_once = True
    repo, state, _, kwargs = _dispatch(tmp_path, monkeypatch, client)
    with pytest.raises(manual_dispatch.DispatchError, match="unavailable"):
        manual_dispatch.dispatch(repo, "docs/design/assignments/test.json", **kwargs)
    before = next(state.glob("*.json")).read_bytes()
    assert json.loads(before)["base_ref"] == "refs/heads/dev-003"
    with pytest.raises(manual_dispatch.DispatchError, match="durable intent"):
        manual_dispatch.dispatch(repo, "docs/design/assignments/test.json",
                                 base_ref="refs/heads/dev-004", **kwargs)
    assert next(state.glob("*.json")).read_bytes() == before
    assert len(client.calls) == 1
