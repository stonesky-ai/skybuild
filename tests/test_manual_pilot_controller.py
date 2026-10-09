"""Safety checks for the operator's manual pilot setup entry points."""

import json
import stat
import sys
from pathlib import Path
from subprocess import CompletedProcess

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
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
    original = (state / "secrets" / "wonko-token").read_bytes()
    with pytest.raises((OSError, ValueError)):
        init_secrets(state)
    assert (state / "secrets" / "wonko-token").read_bytes() == original


def test_preflight_refuses_existing_serve_configuration(tmp_path, monkeypatch):
    checkout = tmp_path / "skybuild"
    checkout.mkdir()
    monkeypatch.setattr(controller, "_available_gib", lambda: 20)
    monkeypatch.setattr(controller, "_port_free", lambda port: True)
    monkeypatch.setattr(controller.shutil, "disk_usage", lambda path: type("Usage", (), {"free": 20 * 1024 ** 3})())

    def command(*args):
        if args[:3] == ("git", "-C", str(checkout)):
            return CompletedProcess(args, 0, str(checkout) + "\n" if "rev-parse" in args else
                                    "https://github.com/stonesky-ai/skybuild.git\n", "")
        if args[:2] == ("docker", "container"):
            return CompletedProcess(args, 1, "", "missing")
        if args[:2] == ("tailscale", "status"):
            return CompletedProcess(args, 0, json.dumps({"BackendState": "Running"}), "")
        if args[:2] == ("tailscale", "serve"):
            return CompletedProcess(args, 0, json.dumps({"TCP": {"443": {}}}), "")
        return CompletedProcess(args, 0, "present", "")

    monkeypatch.setattr(controller, "_command", command)
    result = controller.preflight(checkout)
    assert result["ready_for_operator_setup"] is False
    assert result["checks"]["serve_empty"]["ok"] is False
    assert result["no_changes_made"] is True
