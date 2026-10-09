"""Offline listener tests use committed briefs, fake clocks, and injected clients."""

import json

import pytest

from scripts.manual_pilot_listener import ListenerError, listen
from skybuild.client import ClientError
from test_manual_assignment import pinned  # noqa: F401


class Clock:
    now = 100.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        assert 0 < seconds <= 1
        self.now += seconds


class Inbox:
    def __init__(self, envelope, destination, clock):
        self.messages = [{"message_id": "assignment-1", "sender": envelope["dispatcher"],
                          "recipient": envelope["worker"], "category": "manual-work",
                          "body": json.dumps(envelope)}]
        self.destination = destination
        self.clock = clock
        self.waits = []
        self.receipts = []
        self.lose_receipt = False

    def whoami(self):
        return {"principal_id": "wonko", "is_admin": False,
                "grants": {"skybuild": ["tasks:read", "cord:read", "cord:send", "cord:handle"]}}

    def inbox(self, project, *, limit, offset, wait_seconds):
        assert (project, limit, offset) == ("skybuild", 100, 0)
        assert 0 <= wait_seconds <= 25
        self.waits.append(wait_seconds)
        return self.messages

    def get_task(self, project, task_id):
        return {"task_id": task_id, "status": "ready", "revision": 2}

    def message_action(self, project, message_id, action, *, idempotency_key):
        assert self.destination.exists()
        assert self.destination.stat().st_mode & 0o777 == 0o600
        assert action == "receipt"
        self.receipts.append((message_id, idempotency_key))
        if self.lose_receipt:
            raise ClientError("unavailable", "lost receipt response")
        return {}


@pytest.fixture
def armed(pinned, tmp_path):
    repo, envelope = pinned
    envelope = envelope | {"schema": "manual-work-v2", "task_status": "ready", "task_revision": 2}
    clock = Clock()
    destination = tmp_path / "assignment.json"
    client = Inbox(envelope, destination, clock)
    options = dict(project="skybuild", worker=envelope["worker"], dispatcher=envelope["dispatcher"],
                   assignment_id=envelope["assignment_id"], checkout=repo, base_sha=envelope["base_sha"],
                   brief_path=envelope["brief_path"], brief_sha256=envelope["brief_sha256"],
                   destination=destination, state_dir=tmp_path / "state", deadline=130, duration=30,
                   clock=clock, monotonic=clock, sleep=clock.sleep)
    return client, options, envelope


def test_instant_assignment_saves_before_receipt_and_exits(armed):
    client, options, envelope = armed
    assert listen(client, **options) == {"status": "received", "assignment_id": envelope["assignment_id"],
                                         "message_id": "assignment-1"}
    assert len(client.waits) == len(client.receipts) == 1
    assert json.loads(options["destination"].read_text()) == envelope


@pytest.mark.parametrize("messages", [[], [{"body": "not JSON"}],
                                    [{"body": '{"assignment_id":"unrelated"}'}]])
def test_unrelated_inbox_backs_off_until_bounded_timeout(armed, messages):
    client, options, _ = armed
    client.messages = messages
    options["duration"] = 3
    assert listen(client, **options)["status"] == "timeout"
    assert len(client.waits) == 3
    assert client.receipts == []
    assert not options["destination"].exists()


@pytest.mark.parametrize("change", [{"sender": "foreign"}, {"recipient": "foreign"}, {"category": "other"},
                                   {"body": "schema"}, {"body": "base"}, {"body": "brief"}])
def test_invalid_expected_assignment_never_acknowledged(armed, change):
    client, options, envelope = armed
    if change.get("body") in {"schema", "base", "brief"}:
        key = {"schema": "schema", "base": "base_sha", "brief": "brief_sha256"}[change["body"]]
        change = {"body": json.dumps(envelope | {key: "wrong"})}
    client.messages[0].update(change)
    with pytest.raises(ValueError):
        listen(client, **options)
    assert client.receipts == []
    assert not options["destination"].exists()


def test_lost_receipt_retries_same_durable_snapshot_and_key(armed):
    client, options, _ = armed
    client.lose_receipt = True
    with pytest.raises(ClientError):
        listen(client, **options)
    before = options["destination"].read_bytes()
    client.lose_receipt = False
    assert listen(client, **options)["status"] == "received"
    assert options["destination"].read_bytes() == before
    assert client.receipts[0] == client.receipts[1]


def test_deadline_during_persistence_preserves_file_without_receipt(armed, monkeypatch):
    import skybuild.manual_cord as cord
    client, options, _ = armed
    original = cord._private_write
    def expire_after_save(path, payload):
        original(path, payload)
        client.clock.now = options["deadline"]
    monkeypatch.setattr(cord, "_private_write", expire_after_save)
    with pytest.raises(ListenerError, match="before receipt"):
        listen(client, **options)
    assert options["destination"].exists()
    assert client.receipts == []


def test_checkout_must_remain_clean(armed):
    client, options, _ = armed
    (options["checkout"] / "foreign.txt").write_text("unowned")
    with pytest.raises(ListenerError, match="clean pinned"):
        listen(client, **options)
    assert client.waits == []


def test_another_listener_for_worker_is_refused(armed, monkeypatch):
    import scripts.manual_pilot_listener as module
    client, options, _ = armed
    def locked(*_args):
        raise BlockingIOError()
    monkeypatch.setattr(module.fcntl, "flock", locked)
    with pytest.raises(ListenerError, match="already armed"):
        listen(client, **options)
    assert client.waits == []


def test_absolute_deadline_ends_wait_even_with_longer_duration(armed):
    client, options, _ = armed
    client.messages = []
    options["deadline"] = 102
    assert listen(client, **options)["status"] == "timeout"
    assert len(client.waits) == 2


def test_monotonic_limit_survives_wall_clock_rollback(armed):
    client, options, _ = armed
    client.messages = []
    options["duration"] = 2
    options["clock"] = lambda: 100.0
    assert listen(client, **options)["status"] == "timeout"
    assert len(client.waits) == 2


def test_shorter_approval_remains_monotonically_bounded_after_rollback(armed):
    client, options, _ = armed
    client.messages = []
    options.update(duration=6, deadline=102)
    options["clock"] = lambda: 100.0
    assert listen(client, **options)["status"] == "timeout"
    assert client.clock.now == 102
    assert len(client.waits) == 2


def test_receipt_after_original_approval_is_refused_despite_wall_rollback(armed, monkeypatch):
    import skybuild.manual_cord as cord
    client, options, _ = armed
    options.update(duration=6, deadline=102, clock=lambda: 100.0)
    original = cord._private_write
    def expire_after_save(path, payload):
        original(path, payload)
        client.clock.now = 103
    monkeypatch.setattr(cord, "_private_write", expire_after_save)
    with pytest.raises(ListenerError, match="before receipt"):
        listen(client, **options)
    assert options["destination"].exists()
    assert client.receipts == []


def test_arrival_after_wait_deadline_is_not_saved_or_receipted(armed):
    client, options, _ = armed
    original = client.inbox
    def late(*args, **kwargs):
        result = original(*args, **kwargs)
        client.clock.now = options["deadline"]
        return result
    client.inbox = late
    assert listen(client, **options)["status"] == "timeout"
    assert not options["destination"].exists()
    assert client.receipts == []


@pytest.mark.parametrize("change", [{"duration": 601}, {"duration": 0}, {"deadline": 100},
                                   {"deadline": float("inf")}, {"base_sha": "bad"},
                                   {"brief_sha256": "bad"}])
def test_invalid_arm_never_reads_inbox(armed, change):
    client, options, _ = armed
    with pytest.raises(ListenerError):
        listen(client, **(options | change))
    assert client.waits == []


def test_duplicate_expected_deliveries_are_not_receipted(armed):
    client, options, _ = armed
    client.messages.append(client.messages[0] | {"message_id": "assignment-2"})
    with pytest.raises(ListenerError, match="exactly one"):
        listen(client, **options)
    assert client.receipts == []


@pytest.mark.parametrize("approved_until", ["2026-10-09T15:20:53", "2026-10-09T15:20:53+02:00",
                                           "2000-01-01T00:00:00Z"])
def test_cli_rejects_non_utc_or_expired_approval_before_preflight(armed, approved_until, monkeypatch, capsys):
    import scripts.manual_pilot_listener as module
    _, options, _ = armed
    def forbidden(*_args, **_kwargs):
        pytest.fail("Invalid approval must not reach preflight")
    monkeypatch.setattr(module, "probe_private_api", forbidden)
    args = []
    values = {"url": "https://controller.ts.net", "project": "skybuild", "worker": "wonko",
              "dispatcher": "jeltz", "assignment-id": "pilot-001", "base-sha": options["base_sha"],
              "brief-path": options["brief_path"], "brief-sha256": options["brief_sha256"],
              "approved-until": approved_until, "token-file": "/private/token", "ca-file": "/approved/ca.pem",
              "checkout": str(options["checkout"]), "destination": str(options["destination"]),
              "state-dir": str(options["state_dir"])}
    for key, value in values.items():
        args.extend(("--" + key, value))
    assert module.main(args) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "blocked"


@pytest.mark.parametrize("change", ["token", "ca", "rollback-expiry"])
def test_cli_binds_original_approval_and_credential_material(armed, tmp_path, monkeypatch, capsys, change):
    import scripts.manual_pilot_listener as module
    client, options, _ = armed
    token, ca = tmp_path / "token", tmp_path / "ca.pem"
    token.write_text("x" * 32)
    token.chmod(0o600)
    ca.write_text("original-public-ca")
    ca_digest = module.ca_file_sha256(ca)
    monotonic = Clock()
    wall = Clock()
    monkeypatch.setattr(module.time, "time", wall)
    monkeypatch.setattr(module.time, "monotonic", monotonic)

    def preflight(*_args, **_kwargs):
        if change == "token":
            token.write_text("y" * 32)
        elif change == "ca":
            ca.write_text("replacement-public-ca")
        else:
            monotonic.now = 103
            wall.now = 1

    constructed = []
    actual_client = module.Client
    def factory(_url, actual_token, **kwargs):
        constructed.append(actual_token)
        assert actual_token == "x" * 32
        assert kwargs["expected_ca_sha256"] == ca_digest
        # Exercise the real Client CA digest guard before TLS/network setup.
        if change == "ca":
            return actual_client(_url, actual_token, **kwargs)
        pytest.fail("Expired approval and changed files must not reach receiving client")

    monkeypatch.setattr(module, "probe_private_api", preflight)
    monkeypatch.setattr(module, "Client", factory)
    values = {"url": "https://controller.ts.net", "project": "skybuild", "worker": "wonko",
              "dispatcher": "jeltz", "assignment-id": "pilot-001", "base-sha": options["base_sha"],
              "brief-path": options["brief_path"], "brief-sha256": options["brief_sha256"],
              "approved-until": "1970-01-01T00:01:42Z", "duration": "6", "token-file": str(token),
              "ca-file": str(ca), "checkout": str(options["checkout"]),
              "destination": str(options["destination"]), "state-dir": str(options["state_dir"])}
    args = [arg for key, value in values.items() for arg in ("--" + key, value)]
    assert module.main(args) == 1
    assert len(constructed) == (1 if change == "ca" else 0)
    assert client.waits == client.receipts == []
    assert json.loads(capsys.readouterr().out)["status"] == "blocked"


@pytest.mark.parametrize("identity", [
    {"principal_id": "foreign", "is_admin": False, "grants": {"skybuild": ["cord:read", "cord:send", "cord:handle"]}},
    {"principal_id": "wonko", "is_admin": True, "grants": {"skybuild": ["cord:read", "cord:send", "cord:handle"]}},
    {"principal_id": "wonko", "is_admin": False, "grants": {"skybuild": ["cord:read", "tasks:write"]}},
])
def test_actual_receiving_client_requires_exact_identity_and_scopes(armed, identity):
    client, options, _ = armed
    client.whoami = lambda: identity
    with pytest.raises(ListenerError):
        listen(client, **options)
    assert client.waits == client.receipts == []


def test_receiving_identity_is_rechecked_before_receipt(armed):
    client, options, _ = armed
    original = client.whoami()
    identities = iter([original, original, original | {"principal_id": "foreign"}])
    client.whoami = lambda: next(identities)
    with pytest.raises(ListenerError, match="scoped worker"):
        listen(client, **options)
    assert options["destination"].exists()
    assert client.receipts == []


def test_preflight_original_monotonic_endpoint_cannot_be_rearmed(armed):
    client, options, _ = armed
    options.update(duration=6, deadline=102, clock=lambda: 1.0, monotonic_deadline=102)
    client.clock.now = 103
    assert listen(client, **options)["status"] == "timeout"
    assert client.waits == client.receipts == []
