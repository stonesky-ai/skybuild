"""CPU-only host watch state and duplicate-loop checks."""
import fcntl
import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import stat
import subprocess
from types import SimpleNamespace


SCRIPT = Path(__file__).parents[1] / "scripts/host_watch.py"
spec = importlib.util.spec_from_file_location("skybuild_host_watch", SCRIPT)
watcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watcher)


def test_low_memory_sample_is_written_atomically(tmp_path, monkeypatch):
    original = Path.read_text

    def meminfo(path, *args, **kwargs):
        if str(path) == "/proc/meminfo":
            return "MemAvailable: 3145728 kB\nSwapFree: 1048576 kB\n"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", meminfo)
    state = watcher.sample(4 * 1024**3)
    target = tmp_path / "memory.json"
    watcher.write_state(target, state)

    saved = json.loads(target.read_text())
    assert saved["status"] == "low"
    assert saved["headroom_bytes"] == -(1024**3)
    assert saved["swap_free_bytes"] == 1024**3
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert list(tmp_path.iterdir()) == [target]


def test_second_watcher_exits_without_changing_state(tmp_path):
    target = tmp_path / "memory.json"
    lock = os.open(str(target) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert watcher.watch(target, 4 * 1024**3, 60, 1) == 2
        assert not target.exists()
    finally:
        os.close(lock)


def test_low_disk_is_reported_without_cleanup(tmp_path, monkeypatch):
    original = Path.read_text

    def meminfo(path, *args, **kwargs):
        if str(path) == "/proc/meminfo":
            return "MemAvailable: 8388608 kB\n"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", meminfo)
    monkeypatch.setattr(watcher.shutil, "disk_usage", lambda path: SimpleNamespace(free=2 * 1024**3))
    monkeypatch.setattr(watcher, "docker_gate_status", lambda: {"status": "clear", "count": 0})
    state = watcher.sample(4 * 1024**3, disk_paths=[tmp_path])

    assert state["status"] == "low"
    assert state["disks"][0]["free_bytes"] == 2 * 1024**3
    assert state["headroom_bytes"] == 4 * 1024**3


def test_old_owned_container_is_reported_without_inspecting_secrets(monkeypatch):
    created = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    calls = []

    def fake(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ["systemctl", "is-active"]:
            return subprocess.CompletedProcess(argv, 0)
        if argv[:2] == ["docker", "ps"]:
            return subprocess.CompletedProcess(argv, 0, "abc123\n")
        fields = "\t".join(json.dumps(value) for value in ("/skybuild-gate-test", created, "running"))
        return subprocess.CompletedProcess(argv, 0, fields + "\n")

    monkeypatch.setattr(watcher.subprocess, "run", fake)
    state = watcher.docker_gate_status()

    assert state["status"] == "attention"
    assert state["leftover_count"] == 1
    assert state["containers"][0]["age_seconds"] >= 20 * 60
    assert all("Env" not in " ".join(call) for call in calls)


def test_prunable_worktree_is_reported_read_only(tmp_path, monkeypatch):
    listing = "worktree /source\nHEAD abc\nbranch refs/heads/dev-002\n\nworktree /old\nHEAD def\nprunable gitdir file points to non-existent location\n"
    monkeypatch.setattr(watcher.subprocess, "run", lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, listing))

    state = watcher.worktree_status(tmp_path)

    assert state == {"status": "attention", "count": 2, "prunable_count": 1}
