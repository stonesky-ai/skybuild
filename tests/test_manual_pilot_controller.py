"""Safety checks for the operator's manual pilot setup entry points."""

import json
import stat
import sys
from pathlib import Path
from subprocess import CompletedProcess

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import manual_pilot_provision as provisioner  # noqa: E402
from manual_pilot_provision import init_secrets  # noqa: E402
import manual_pilot_controller as controller  # noqa: E402


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
