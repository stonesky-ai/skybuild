"""CPU-only host watch state and duplicate-loop checks."""
import fcntl
import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest


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


def test_stop_request_ends_watcher_without_sampling(tmp_path, monkeypatch):
    target = tmp_path / "host.json"
    request = tmp_path / "host.json.stop"
    request.write_text("stop\n")
    monkeypatch.setattr(watcher, "sample", lambda *args, **kwargs: pytest.fail("unexpected sample"))

    assert watcher.watch(target, 4 * 1024**3, 60, 1) == 0
    assert json.loads(target.read_text())["status"] == "stopped"
    assert not request.exists()


def test_low_disk_is_reported_without_cleanup(tmp_path, monkeypatch):
    original = Path.read_text

    def meminfo(path, *args, **kwargs):
        if str(path) == "/proc/meminfo":
            return "MemAvailable: 8388608 kB\n"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", meminfo)
    monkeypatch.setattr(watcher.shutil, "disk_usage", lambda path: SimpleNamespace(free=2 * 1024**3))
    monkeypatch.setattr(watcher, "docker_gate_status", lambda: {"status": "clear", "count": 0})
    monkeypatch.setattr(watcher, "docker_runtime_status", lambda: {"status": "absent", "count": 0})
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

    assert state["status"] == "attention" and state["prunable_count"] == 1
    assert state["records"][1] == {
        "path": "/old", "head": "def", "branch": None,
        "prunable_reason": "gitdir file points to non-existent location", "ownership": "unknown",
    }


@pytest.mark.parametrize("options", [
    ["--interval", "1"], ["--interval", "600"], ["--duration-minutes", "481"],
])
def test_watch_rejects_excessive_polling_or_lifetime(tmp_path, monkeypatch, options):
    monkeypatch.setattr(sys, "argv", ["host_watch.py", "--state", str(tmp_path / "state.json"),
                                      "--watch", *options])
    with pytest.raises(SystemExit) as stopped:
        watcher.main()
    assert stopped.value.code == 2
    assert not (tmp_path / "state.json").exists()


def runtime_inventory(monkeypatch, rows):
    calls = []

    def fake(argv, **kwargs):
        calls.append(argv)
        assert kwargs["timeout"] == 5 and kwargs["check"]
        if argv[:2] == ["docker", "ps"]:
            return subprocess.CompletedProcess(argv, 0, "".join(identifier + "\n" for identifier, _ in rows))
        assert argv[:2] == ["docker", "inspect"]
        return subprocess.CompletedProcess(argv, 0, "".join(
            "\t".join(json.dumps(field) for field in fields) + "\n" for _, fields in rows))

    monkeypatch.setattr(watcher.subprocess, "run", fake)
    return calls


def test_runtime_selection_reports_healthy_owned_services_without_secrets(monkeypatch):
    rows = [("abcdef012345", ["/skybuild-pilot-pg", "running", "healthy", 768 * 1024**2, "skybuild-pilot"]),
            ("123456abcdef", ["/skybuild-pilot-api", "running", "none", 512 * 1024**2, "skybuild-pilot"])]
    calls = runtime_inventory(monkeypatch, rows)
    state = watcher.docker_runtime_status()
    assert state["status"] == "present" and state["count"] == 2
    assert [item["status"] for item in state["containers"]] == ["healthy", "healthy"]
    assert state["containers"][1]["memory_limit_bytes"] == 512 * 1024**2
    assert "label=com.docker.compose.project=skybuild-pilot" in calls[0]
    assert calls[1][-2:] == [row[0] for row in rows]
    assert all(term not in " ".join(calls[1]) for term in (".Config.Env", ".Mounts", ".State.Health.Log"))
    assert "leftover_count" not in state and all("leftover" not in item for item in state["containers"])


@pytest.mark.parametrize("state,health,cap,expected", [
    ("exited", "none", 100, "attention"), ("running", "unhealthy", 100, "attention"),
    ("restarting", "starting", 100, "attention"), ("paused", "none", 100, "attention"),
    ("running", "starting", 100, "attention"), ("running", "none", 0, "unknown"),
    ("running", "none", None, "unknown"), ("running", "invalid", 100, "unknown"),
    ("unexpected", "healthy", 100, "unknown"),
])
def test_runtime_failure_or_missing_evidence_is_visible(monkeypatch, state, health, cap, expected):
    runtime_inventory(monkeypatch, [("abcdef012345", ["/pilot", state, health, cap, "skybuild-pilot"])])
    assert watcher.docker_runtime_status()["status"] == expected


def test_runtime_names_are_sanitized_and_ownership_is_rechecked(monkeypatch):
    runtime_inventory(monkeypatch, [("abcdef012345", ["/pilot\nunsafe", "running", "none", 100, "skybuild-pilot"])])
    assert watcher.docker_runtime_status()["containers"][0]["name"] == "pilot_unsafe"
    runtime_inventory(monkeypatch, [("abcdef012345", ["/other", "running", "none", 100, "other-project"])])
    assert watcher.docker_runtime_status()["status"] == "unknown"


def test_runtime_absence_is_distinct_from_gate_clear(monkeypatch):
    calls = runtime_inventory(monkeypatch, [])
    assert watcher.docker_runtime_status() == {"status": "absent", "count": 0}
    assert len(calls) == 1


@pytest.mark.parametrize("mode", ["unavailable", "timeout", "malformed", "too_many", "missing_row"])
def test_runtime_inventory_errors_are_bounded_and_unknown(monkeypatch, mode):
    calls = []

    def fake(argv, **kwargs):
        calls.append(argv)
        if mode == "unavailable":
            raise subprocess.CalledProcessError(1, argv, stderr="private diagnostic")
        if mode == "timeout":
            raise subprocess.TimeoutExpired(argv, 5)
        if argv[:2] == ["docker", "ps"]:
            output = "invalid-id\n" if mode == "malformed" else "abcdef012345\n" * (33 if mode == "too_many" else 1)
            return subprocess.CompletedProcess(argv, 0, output)
        return subprocess.CompletedProcess(argv, 0, "")

    monkeypatch.setattr(watcher.subprocess, "run", fake)
    result = watcher.docker_runtime_status()
    assert result["status"] == "unknown" and "private diagnostic" not in json.dumps(result)
    assert len(calls) == (2 if mode == "missing_row" else 1)


@pytest.mark.parametrize("runtime,expected", [("present", "ok"), ("attention", "attention"), ("unknown", "unknown")])
def test_sample_combines_runtime_health_without_changing_gate_leftovers(tmp_path, monkeypatch, runtime, expected):
    original = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda path, *args, **kwargs:
                        "MemAvailable: 16777216 kB\n" if str(path) == "/proc/meminfo" else original(path, *args, **kwargs))
    monkeypatch.setattr(watcher, "disk_status", lambda *args: [{"status": "ok"}])
    monkeypatch.setattr(watcher, "docker_gate_status", lambda: {"status": "clear", "count": 0, "leftover_count": 0})
    monkeypatch.setattr(watcher, "docker_runtime_status", lambda: {"status": runtime, "count": 2})
    sample = watcher.sample(8 * 1024**3, disk_paths=[tmp_path])
    assert sample["status"] == expected
    assert sample["docker_gate"]["leftover_count"] == 0 and sample["docker_runtime"]["count"] == 2
