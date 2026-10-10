"""Safety checks for the operator's manual pilot setup entry points."""

import json
import stat
import sys
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import manual_pilot_provision as provisioner  # noqa: E402
from manual_pilot_provision import init_secrets  # noqa: E402
import manual_pilot_controller as controller  # noqa: E402
import manual_pilot_tls as tls  # noqa: E402


def test_promotion_accepts_only_additive_012_authority_migration():
    current = {f"migrations/{version:03d}_migration.sql": str(version) * 64
               for version in range(1, 12)}
    candidate = {**current, "migrations/012_api_task_authority.sql": "f" * 64}
    controller._verify_schema_011_to_012(current, candidate)
    changed = {**candidate, "migrations/011_migration.sql": "e" * 64}
    with pytest.raises(ValueError, match="unchanged schema 001-011"):
        controller._verify_schema_011_to_012(current, changed)


def test_promotion_accepts_only_exact_schema_013_to_016_migration_set():
    current = {f"migrations/{version:03d}_migration.sql": f"{version:064x}"
               for version in range(1, 14)}
    migrations = controller.SCHEMA_TRANSITIONS["013-to-016"][2]
    candidate = {**current, **{name: "f" * 64 for name in migrations}}
    assert controller._verify_schema_transition(current, candidate, "013-to-016") == (13, 16)

    changed_prefix = {**candidate, "migrations/013_migration.sql": "e" * 64}
    missing_middle = dict(candidate)
    del missing_middle[migrations[1]]
    wrong_name = dict(candidate)
    wrong_name["migrations/015_unreviewed.sql"] = wrong_name.pop(migrations[1])
    extra_migration = {**candidate, "migrations/017_unreviewed.sql": "a" * 64}
    for invalid in (changed_prefix, missing_middle, wrong_name, extra_migration):
        with pytest.raises(ValueError, match="unchanged schema 001-013"):
            controller._verify_schema_transition(current, invalid, "013-to-016")
    with pytest.raises(ValueError, match="explicit reviewed"):
        controller._verify_schema_transition(current, candidate, "013-to-017")


def test_secret_initialization_is_private_and_never_rotates(tmp_path):
    state = tmp_path / "new-pilot-state"
    result = init_secrets(state)
    assert result["initialized"] is True
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert stat.S_IMODE((state / "secrets").stat().st_mode) == 0o700
    assert stat.S_IMODE((state / "secrets" / "admin-password").stat().st_mode) == 0o644
    assert stat.S_IMODE((state / "secrets" / "wonko-token").stat().st_mode) == 0o600
    assert stat.S_IMODE((state / "secrets" / "pilot_dispatcher-token").stat().st_mode) == 0o600
    original = (state / "secrets" / "wonko-token").read_bytes()
    with pytest.raises((OSError, ValueError)):
        init_secrets(state)
    assert (state / "secrets" / "wonko-token").read_bytes() == original


def test_preflight_refuses_existing_serve_configuration(tmp_path, monkeypatch):
    checkout = tmp_path / "skybuild"
    checkout.mkdir()
    approved_sha = "a" * 40
    monkeypatch.setattr(controller, "_available_gib", lambda: 20)
    monkeypatch.setattr(controller, "_port_free", lambda port: True)
    monkeypatch.setattr(controller.shutil, "disk_usage", lambda path: type("Usage", (), {"free": 20 * 1024 ** 3})())

    def command(*args):
        if args[:3] == ("git", "-C", str(checkout)):
            if "rev-parse" in args:
                return CompletedProcess(args, 0, (str(checkout) if "--show-toplevel" in args else approved_sha) + "\n", "")
            if "status" in args:
                return CompletedProcess(args, 0, "", "")
            if "ls-remote" in args:
                return CompletedProcess(args, 0, approved_sha + "\trefs/heads/dev-002\n", "")
            return CompletedProcess(args, 0, "https://github.com/stonesky-ai/skybuild.git\n", "")
        if args[:2] == ("docker", "container"):
            return CompletedProcess(args, 1, "", "missing")
        if args[:2] == ("tailscale", "status"):
            return CompletedProcess(args, 0, json.dumps({"BackendState": "Running"}), "")
        if args[:2] == ("tailscale", "serve"):
            return CompletedProcess(args, 0, json.dumps({"TCP": {"443": {}}}), "")
        return CompletedProcess(args, 0, "present", "")

    monkeypatch.setattr(controller, "_command", command)
    result = controller.preflight(checkout, approved_sha, "refs/heads/dev-002")
    assert result["ready_for_operator_setup"] is False
    assert result["checks"]["serve_empty"]["ok"] is False
    assert result["checks"]["published_clean_head"]["ok"] is True
    assert result["no_changes_made"] is True


def test_preflight_rejects_dirty_or_unpublished_head(tmp_path, monkeypatch):
    checkout = tmp_path / "skybuild"
    checkout.mkdir()
    approved_sha = "a" * 40
    monkeypatch.setattr(controller, "_available_gib", lambda: 20)
    monkeypatch.setattr(controller, "_port_free", lambda port: True)
    monkeypatch.setattr(controller.shutil, "disk_usage", lambda path: type("Usage", (), {"free": 20 * 1024 ** 3})())

    def command(*args):
        if args[0] == "git":
            if "--show-toplevel" in args:
                return CompletedProcess(args, 0, str(checkout) + "\n", "")
            if "rev-parse" in args:
                return CompletedProcess(args, 0, approved_sha + "\n", "")
            if "status" in args:
                return CompletedProcess(args, 0, "?? src/skybuild/extra.py\n", "")
            if "ls-remote" in args:
                return CompletedProcess(args, 0, "b" * 40 + "\trefs/heads/dev-002\n", "")
            return CompletedProcess(args, 0, "https://github.com/stonesky-ai/skybuild.git\n", "")
        if args[:2] == ("docker", "container"):
            return CompletedProcess(args, 1, "", "missing")
        if args[:2] == ("tailscale", "status"):
            return CompletedProcess(args, 0, json.dumps({"BackendState": "Running"}), "")
        return CompletedProcess(args, 0, "{}", "")

    monkeypatch.setattr(controller, "_command", command)
    report = controller.preflight(checkout, approved_sha, "refs/heads/dev-002")
    assert report["checks"]["published_clean_head"]["ok"] is False


def test_tls_controller_accepts_explicit_retained_compose_owner_path(tmp_path, monkeypatch):
    checkout = tmp_path / 'source-clone'
    checkout.mkdir()
    state = tmp_path / 'private-state'
    state.mkdir(mode=0o700)
    owner = tmp_path / 'retained-main-checkout/ops/manual-pilot'
    owner.mkdir(parents=True)
    sha = 'a' * 40
    image = 'sha256:' + 'b' * 64
    hostname = 'controller.tail.ts.net'
    ip = '100.100.1.2'
    rows = {
        'skybuild-pilot-api': {
            'Config': {'Labels': {'com.docker.compose.project': 'skybuild-pilot',
                                  'com.docker.compose.service': 'api',
                                  'com.docker.compose.project.working_dir': str(owner)}},
            'State': {'Running': True}, 'Image': image, 'Mounts': [],
            'NetworkSettings': {'Ports': {}}},
        'skybuild-pilot-pg': {
            'Config': {'Labels': {'com.docker.compose.project': 'skybuild-pilot',
                                  'com.docker.compose.service': 'db',
                                  'com.docker.compose.project.working_dir': str(owner)}},
            'State': {'Running': True}, 'Image': 'sha256:' + 'c' * 64,
            'NetworkSettings': {'Ports': {'5432/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '55432'}]}},
            'Mounts': [{'Destination': '/var/lib/postgresql/data', 'Source': str(state / 'pgdata')}]},
    }

    def command(*args):
        if args[:4] == ('git', '-C', str(checkout), 'rev-parse'):
            return str(checkout) if '--show-toplevel' in args else sha
        if args[:4] == ('git', '-C', str(checkout), 'remote'):
            return 'https://github.com/stonesky-ai/skybuild.git'
        if args[:4] == ('git', '-C', str(checkout), 'status'):
            return ''
        if args[:4] == ('git', '-C', str(checkout), 'ls-remote'):
            return sha + '\trefs/heads/dev-017'
        if args[:3] == ('tailscale', 'status', '--json'):
            return json.dumps({'BackendState': 'Running', 'Self': {
                'DNSName': hostname, 'TailscaleIPs': [ip]}})
        if args[:2] == ('docker', 'inspect'):
            return json.dumps([rows[args[2]]])
        if args[:3] == ('docker', 'image', 'inspect'):
            return image if args[3] == 'skybuild-pilot-api:local' else 'sha256:' + 'c' * 64
        raise AssertionError(args)

    monkeypatch.setattr(tls, 'command', command)
    tls.controller(checkout, sha, state, hostname, ip, expected_api_image=image,
                   published_ref='refs/heads/dev-017', compose_owner_path=owner)


def test_tls_controller_rejects_compose_owner_symlink(tmp_path, monkeypatch):
    checkout = tmp_path / 'source-clone'
    checkout.mkdir()
    state = tmp_path / 'private-state'
    state.mkdir(mode=0o700)
    real = tmp_path / 'retained/ops/manual-pilot-real'
    real.mkdir(parents=True)
    owner = tmp_path / 'retained/ops/manual-pilot'
    owner.symlink_to(real, target_is_directory=True)
    with pytest.raises(tls.TLSError, match='exact retained Compose'):
        tls.controller(checkout, 'a' * 40, state, 'controller.tail.ts.net', '100.100.1.2',
                       compose_owner_path=owner)


def test_provision_refuses_wrong_port_or_state_before_connect(tmp_path, monkeypatch):
    state = tmp_path / "pilot"
    init_secrets(state)
    inspected = {
        "Config": {"Labels": {"com.docker.compose.project": "skybuild-pilot",
                              "com.docker.compose.service": "db"}, "Image": "postgres:16"},
        "State": {"Running": True, "Health": {"Status": "healthy"}},
        "Image": "sha256:reviewed",
        "HostConfig": {"PortBindings": {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "55432"}]},
                       "Memory": 768 * 1024 ** 2, "PidsLimit": 128},
        "NetworkSettings": {"Ports": {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "55432"}]}},
        "Mounts": [{"Type": "bind", "Source": str(state / "pgdata"),
                    "Destination": "/var/lib/postgresql/data", "RW": True},
                   {"Type": "bind", "Source": str(state / "secrets" / "admin-password"),
                    "Destination": "/run/secrets/admin-password", "RW": False}],
    }

    def run(args, **kwargs):
        if args[:3] == ["docker", "container", "inspect"]:
            return CompletedProcess(args, 0, json.dumps(inspected), "")
        if args[:3] == ["docker", "image", "inspect"]:
            return CompletedProcess(args, 0, "sha256:reviewed\n", "")
        if args[:2] == ["docker", "exec"]:
            return CompletedProcess(args, 0, "12345\n", "")
        raise AssertionError(args)

    monkeypatch.setattr(provisioner.subprocess, "run", run)
    assert provisioner._dedicated_container(state) == "12345"
    inspected["HostConfig"]["PortBindings"]["5432/tcp"][0]["HostPort"] = "9999"
    with pytest.raises(ValueError, match="loopback port"):
        provisioner._dedicated_container(state)
    inspected["HostConfig"]["PortBindings"]["5432/tcp"][0]["HostPort"] = "55432"
    inspected["Mounts"][0]["Source"] = str(tmp_path / "foreign-pgdata")
    with pytest.raises(ValueError, match="state directory"):
        provisioner._dedicated_container(state)


def test_provision_refuses_mismatched_database_system_identifier():
    class WrongDatabase:
        def execute(self, statement):
            assert statement == "SELECT system_identifier FROM pg_control_system()"
            return self

        def fetchone(self):
            return (99999,)

    with pytest.raises(ValueError, match="not the inspected pilot"):
        provisioner._require_same_cluster(WrongDatabase(), "12345")


@pytest.fixture
def provision_with_existing_store(tmp_path, monkeypatch):
    """Exercise provisioning policy while isolating container/database creation."""
    state = tmp_path / "pilot"
    init_secrets(state)

    class BootstrapConnection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, *args):
            return self

        def fetchone(self):
            return None

    monkeypatch.setattr(provisioner, "_dedicated_container", lambda path: "12345")
    monkeypatch.setattr(provisioner, "_require_same_cluster", lambda *args: None)
    monkeypatch.setattr(provisioner, "psycopg", SimpleNamespace(connect=lambda *args, **kwargs: BootstrapConnection()))
    monkeypatch.setattr(provisioner, "provision_runtime_role", lambda *args: {"ok": True})
    monkeypatch.setattr(provisioner, "audit_runtime_role", lambda *args: {"ok": True})

    def run(store):
        monkeypatch.setattr(provisioner, "Store", lambda *args: store)
        assert provisioner.provision(state)["provisioned"] is True
        return state

    return run


def test_provision_dispatcher_has_only_project_scoped_grants(provision_with_existing_store):
    class RecordingStore:
        def __init__(self):
            self.principals = {}

        def migrate(self):
            pass

        def provision_principal(self, principal, token, *, is_admin=False, grants=None):
            self.principals[principal] = (is_admin, grants)

    store = RecordingStore()
    provision_with_existing_store(store)
    assert store.principals["pilot_dispatcher"] == (
        False, {"skybuild": {"tasks:read", "cord:read", "cord:send", "cord:handle"}})
    assert set(store.principals) == {"pilot_owner", "pilot_dispatcher", "wonko", "wowbagger"}
    assert store.principals["pilot_owner"] == (True, None)
    for worker in ("wonko", "wowbagger"):
        assert store.principals[worker] == (
            False, {"skybuild": {"tasks:read", "cord:read", "cord:send", "cord:handle"}})


def test_provisioned_dispatcher_receives_worker_result_over_api(
        provision_with_existing_store, restricted_database):
    from fastapi.testclient import TestClient
    from skybuild.api import create_app
    from skybuild.store import Store

    admin_dsn, runtime_dsn, database, _ = restricted_database
    state = provision_with_existing_store(Store(admin_dsn, database))
    runtime = Store(runtime_dsn, database)

    def headers(principal):
        token = (state / "secrets" / f"{principal}-token").read_text().strip()
        return {"Authorization": f"Bearer {token}", "Idempotency-Key": uuid4().hex}

    dispatcher = headers("pilot_dispatcher")
    with TestClient(create_app(runtime)) as client:
        identity = client.get("/api/v1/me", headers=dispatcher)
        assert identity.status_code == 200
        assert identity.json() == {"principal_id": "pilot_dispatcher", "is_admin": False,
                                   "grants": {"skybuild": ["cord:handle", "cord:read", "cord:send", "tasks:read"]}}
        base = "/api/v1/projects/skybuild/cord"
        assignment = client.post(base + "/messages", headers=dispatcher, json={
            "recipient": "wonko", "subject": "Pilot assignment", "category": "manual-work",
            "body": json.dumps({"schema": "manual-work-v1", "assignment_id": uuid4().hex})})
        assert assignment.status_code == 201
        worker = headers("wonko")
        result = client.post(base + "/messages", headers=worker, json={
            "recipient": "pilot_dispatcher", "subject": "Pilot result", "category": "manual-work",
            "reply_to": assignment.json()["message_id"],
            "body": json.dumps({"schema": "manual-result-v1", "phase": "ready-for-review"})})
        assert result.status_code == 201
        message_id = result.json()["message_id"]
        inbox = client.get(base + "/inbox", headers=dispatcher)
        assert inbox.status_code == 200
        assert message_id in {message["message_id"] for message in inbox.json()}
        for action, timestamp in (("receipt", "delivered_at"), ("handle", "handled_at")):
            response = client.post(base + f"/messages/{message_id}/{action}",
                                   headers=headers("pilot_dispatcher"), json={})
            assert response.status_code == 200
            assert response.json()[timestamp] is not None
        tasks = client.get("/api/v1/projects/skybuild/tasks", headers=dispatcher)
        assert tasks.status_code == 200 and tasks.json() == []
        assert client.post("/api/v1/projects/skybuild/tasks", headers=dispatcher, json={
            "task_id": "FORBIDDEN", "title": "Forbidden", "description": "No write scope"}).status_code == 403
        assert client.get("/api/v1/projects/other/cord/inbox", headers=dispatcher).status_code == 403
        forbidden = client.post(base + f"/messages/{assignment.json()['message_id']}/handle",
                                headers=headers("pilot_dispatcher"), json={})
        assert forbidden.status_code == 403
