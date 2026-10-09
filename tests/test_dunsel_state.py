from __future__ import annotations

import json
import os
import re
import stat
import subprocess

import pytest

from skybuild import dunsel_state, marshall_dunsel
from skybuild.workbench import marshalls


def use_private_state(monkeypatch, path):
    monkeypatch.setattr(dunsel_state, "STATE_DIR", path)


def test_state_directory_and_files_are_private(tmp_path, monkeypatch):
    use_private_state(monkeypatch, tmp_path / "state")

    dunsel_state.write_text(dunsel_state.PID_FILE, "123\n")

    assert stat.S_IMODE(dunsel_state.STATE_DIR.stat().st_mode) == 0o700
    assert stat.S_IMODE((dunsel_state.STATE_DIR / dunsel_state.PID_FILE).stat().st_mode) == 0o600
    assert dunsel_state.read_text(dunsel_state.PID_FILE) == "123\n"


def test_state_file_symlink_cannot_overwrite_target(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    use_private_state(monkeypatch, state_dir)
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me", encoding="utf-8")
    (state_dir / dunsel_state.LOG_FILE).symlink_to(victim)

    with pytest.raises(OSError):
        dunsel_state.write_text(dunsel_state.LOG_FILE, "overwrite")

    assert victim.read_text(encoding="utf-8") == "keep me"


def test_state_file_hardlink_cannot_overwrite_target(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    use_private_state(monkeypatch, state_dir)
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me", encoding="utf-8")
    os.link(victim, state_dir / dunsel_state.EXIT_FILE)

    with pytest.raises(PermissionError, match="unsafe Dunsel state file"):
        dunsel_state.write_text(dunsel_state.EXIT_FILE, "overwrite")

    assert victim.read_text(encoding="utf-8") == "keep me"


def test_state_fifo_is_rejected_without_blocking(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    use_private_state(monkeypatch, state_dir)
    os.mkfifo(state_dir / dunsel_state.LOG_FILE, mode=0o600)

    with pytest.raises(OSError):
        dunsel_state.write_text(dunsel_state.LOG_FILE, "overwrite")


def test_state_directory_symlink_is_refused(tmp_path, monkeypatch):
    victim_dir = tmp_path / "victim-dir"
    victim_dir.mkdir(mode=0o700)
    state_dir = tmp_path / "state-link"
    state_dir.symlink_to(victim_dir, target_is_directory=True)
    use_private_state(monkeypatch, state_dir)

    with pytest.raises(OSError):
        dunsel_state.write_text(dunsel_state.PID_FILE, "overwrite")

    assert list(victim_dir.iterdir()) == []


def test_disable_blocks_start_and_graceful_stop_writes_private_request(tmp_path, monkeypatch):
    use_private_state(monkeypatch, tmp_path / "state")
    monkeypatch.setattr(marshalls, "_worker_processes", lambda: [])

    marshalls.set_enabled(False)
    with pytest.raises(marshalls.MarshallConflict, match="Dunsel is disabled"):
        marshalls.start()
    assert dunsel_state.file_exists(dunsel_state.DISABLED_FILE)

    marshalls.set_enabled(True)
    monkeypatch.setattr(
        marshalls, "_worker_processes",
        lambda: [{"pid": 123, "command": "fixed dunsel worker"}],
    )
    result = marshalls.graceful_stop()

    assert result == {"requested": True, "pid": 123}
    assert re.fullmatch(r"requested_at=\d{4}-\d\d-\d\dT.*Z\n",
                        dunsel_state.read_text(dunsel_state.EXIT_FILE))


def test_worker_consumes_private_exit_request_and_records_exit(tmp_path, monkeypatch):
    use_private_state(monkeypatch, tmp_path / "state")
    monkeypatch.setattr(marshall_dunsel.sys, "argv", ["marshall_dunsel.py", "--instance", "dunsel"])
    dunsel_state.write_text(dunsel_state.EXIT_FILE, "requested_at=2026-10-09T00:00:00Z\n")

    assert marshall_dunsel.run() == 0

    info, raw = dunsel_state.read_tail(dunsel_state.LOG_FILE, 4096)
    events = [json.loads(line)["event"] for line in raw.decode().splitlines()]
    assert info.st_mtime > 0
    assert events == ["started", "saw exit file"]
    assert not dunsel_state.file_exists(dunsel_state.EXIT_FILE)
    assert not dunsel_state.file_exists(dunsel_state.PID_FILE)


def test_kill_uses_only_fixed_worker_pattern(tmp_path, monkeypatch):
    use_private_state(monkeypatch, tmp_path / "state")
    monkeypatch.setattr(
        marshalls, "_worker_processes",
        lambda: [{"pid": 123, "command": "fixed dunsel worker"}],
    )
    monkeypatch.setattr(marshalls.shutil, "which", lambda name: "/usr/bin/pkill")
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(marshalls.subprocess, "run", fake_run)

    result = marshalls.kill()

    assert result == {"killed": True, "pids": [123]}
    args, kwargs = calls[0]
    assert args[:3] == ["/usr/bin/pkill", "-f", "--"]
    assert args[3] == "^" + re.escape(str(marshalls.sys.executable)) + " " + re.escape(str(marshalls.WORKER)) + r" --instance dunsel$"
    assert kwargs["check"] is False
