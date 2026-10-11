"""Real Store validation of explicit controller dispatch compatibility."""

import hashlib
import json
import pytest
from skybuild import controller_cord_dispatch as manual_dispatch


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


def test_changed_current_brief_cannot_replace_pinned_intent(tmp_path, monkeypatch):
    client = FakeClient()
    repo, _, envelope, kwargs = _dispatch(tmp_path, monkeypatch, client)
    manual_dispatch.dispatch(repo, "docs/design/assignments/test.json", **kwargs)
    envelope["owned_paths"] = ["src/skybuild/other.py"]
    result = manual_dispatch.dispatch(repo, "docs/design/assignments/test.json", **kwargs)
    assert result["mode"] == "already_sent"
    assert len(client.calls) == 1
    assert json.loads(client.calls[0][1]["body"])["owned_paths"] == ["src/skybuild/new_module.py"]


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


class _IdempotencyConnection:
    """Use the real Store validator with an in-memory SQL result boundary."""

    def __init__(self):
        self.rows = {}
        self.row = None

    def execute(self, sql, args):
        if sql.startswith("SELECT payload_hash"):
            self.row = self.rows.get(tuple(args))
        elif sql.startswith("INSERT INTO idempotency"):
            self.rows[tuple(args[:4])] = {"payload_hash": args[4], "response": args[5].obj}
        return self

    def fetchone(self):
        return self.row


def test_new_assignment_revision_uses_real_store_idempotency(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from skybuild.store import Store, DomainError

    connection = _IdempotencyConnection()
    principal = SimpleNamespace(principal_id="pilot_dispatcher")
    store = object.__new__(Store)

    class StoreClient(FakeClient):
        def send_message(self, project, body, *, idempotency_key):
            self.calls.append((project, body, idempotency_key))
            return store._idempotent(connection, principal, project, "cord.send", idempotency_key,
                body, lambda: {"message_id": str(len(connection.rows))})

    client = StoreClient()
    repo, state, _, kwargs = _dispatch(tmp_path, monkeypatch, client)
    manual_dispatch.dispatch(repo, "docs/design/assignments/test.json", **kwargs)
    first = client.calls[-1]
    client.task["revision"] = 5
    kwargs["state_dir"] = tmp_path / "new-state"
    manual_dispatch.dispatch(repo, "docs/design/assignments/test.json", **kwargs)
    second = client.calls[-1]
    assert first[2] != second[2]
    assert len(connection.rows) == 2
    with pytest.raises(DomainError, match="different input"):
        store._idempotent(connection, principal, "skybuild", "cord.send", first[2], second[1], lambda: {})
    manual_dispatch.dispatch(repo, "docs/design/assignments/test.json", **kwargs)
    assert len(client.calls) == 2


def test_legacy_sending_intent_keeps_its_exact_key(tmp_path, monkeypatch):
    client = FakeClient()
    client.fail_once = True
    repo, state, _, kwargs = _dispatch(tmp_path, monkeypatch, client)
    with pytest.raises(manual_dispatch.DispatchError):
        manual_dispatch.dispatch(repo, "docs/design/assignments/test.json", **kwargs)
    path = next(state.glob("*.json"))
    retained = json.loads(path.read_text())
    identity = hashlib.sha256(b"skybuild\0MWP-test-1").hexdigest()
    retained["idempotency_key"] = "manual-work-v1:" + identity
    path.write_text(json.dumps(retained))
    manual_dispatch.dispatch(repo, "docs/design/assignments/test.json", **kwargs)
    assert client.calls[-1][2] == retained["idempotency_key"]
    assert client.calls[-1][1] == retained["message"]
