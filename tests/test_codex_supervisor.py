"""Focused tests for the local cron recovery boundary."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "skybuild_codex_supervisor.py"
SPEC = importlib.util.spec_from_file_location("skybuild_codex_supervisor", SCRIPT)
assert SPEC and SPEC.loader
supervisor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(supervisor)


def prepared(tmp_path: Path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    prompt = tmp_path / "prompt.txt"
    resume = tmp_path / "resume.txt"
    prompt.write_text("Do bounded task.\n")
    resume.write_text("Inspect prior state and continue unfinished work only.\n")
    args = SimpleNamespace(
        checkout=checkout, prompt_file=prompt, resume_file=resume,
        deadline_utc=(supervisor.utc_now() + supervisor.dt.timedelta(minutes=5)).isoformat(),
        request_id="test-request",
    )
    state_file = state_dir / "request.json"
    supervisor.prepare(args, state_file)
    return checkout, state_file, args


def test_tick_is_idle_without_prepared_request(tmp_path: Path):
    assert supervisor.tick(tmp_path / "request.json", Path("/no/codex")) == "idle: no prepared request"


def test_prepare_rejects_duplicate_and_expired_request(tmp_path: Path):
    _, state_file, args = prepared(tmp_path)
    with pytest.raises(ValueError, match="active"):
        supervisor.prepare(args, state_file)
    state_file.unlink()
    args.deadline_utc = (supervisor.utc_now() - supervisor.dt.timedelta(seconds=1)).isoformat()
    with pytest.raises(ValueError, match="deadline"):
        supervisor.prepare(args, state_file)


def test_tick_waits_for_memory_and_live_codex(tmp_path: Path, monkeypatch):
    checkout, state_file, _ = prepared(tmp_path)
    monkeypatch.setattr(supervisor, "available_gib", lambda: 9.9)
    assert supervisor.tick(state_file, Path("/no/codex")).startswith("waiting: less than 10")
    monkeypatch.setattr(supervisor, "available_gib", lambda: 20)
    monkeypatch.setattr(supervisor, "competing_codex", lambda path: [123] if path == checkout else [])
    assert supervisor.tick(state_file, Path("/no/codex")) == "waiting: live Codex session in checkout"
    assert supervisor.load_json(state_file)["runs"] == 0


def test_interrupted_run_without_session_id_parks(tmp_path: Path, monkeypatch):
    _, state_file, _ = prepared(tmp_path)
    state = supervisor.load_json(state_file)
    state.update(phase="running", runs=1, pid=100, start_ticks=7)
    supervisor.atomic_json(state_file, state)
    monkeypatch.setattr(supervisor, "same_child", lambda value: False)
    assert supervisor.tick(state_file, Path("/no/codex")) == "parked"
    assert supervisor.load_json(state_file)["runs"] == 1


def test_completed_run_is_not_replayed(tmp_path: Path, monkeypatch):
    _, state_file, _ = prepared(tmp_path)
    state = supervisor.load_json(state_file)
    state.update(phase="running", runs=1, pid=100, start_ticks=7,
                 session_id="12345678-1234-1234-1234-123456789abc", terminal_event="turn.completed")
    supervisor.atomic_json(state_file, state)
    monkeypatch.setattr(supervisor, "same_child", lambda value: False)
    assert supervisor.tick(state_file, Path("/no/codex")) == "completed"
    assert supervisor.load_json(state_file)["runs"] == 1


def test_low_memory_stops_only_recorded_child(tmp_path: Path, monkeypatch):
    _, state_file, _ = prepared(tmp_path)
    state = supervisor.load_json(state_file)
    state.update(phase="running", runs=1, pid=100, start_ticks=7)
    supervisor.atomic_json(state_file, state)
    stopped = []
    monkeypatch.setattr(supervisor, "same_child", lambda value: True)
    monkeypatch.setattr(supervisor, "available_gib", lambda: 7.9)
    monkeypatch.setattr(supervisor, "stop_owned_child", lambda value: stopped.append(value["pid"]))
    assert supervisor.tick(state_file, Path("/no/codex")) == "memory_stop"
    assert stopped == [100]


def test_fake_codex_runs_once_and_records_completion(tmp_path: Path, monkeypatch):
    _, state_file, _ = prepared(tmp_path)
    fake = tmp_path / "fake_codex"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "assert sys.stdin.read().strip() == 'Do bounded task.'\n"
        "print(json.dumps({'type':'thread.started','thread_id':'12345678-1234-1234-1234-123456789abc'}), flush=True)\n"
        "print(json.dumps({'type':'turn.completed'}), flush=True)\n"
    )
    fake.chmod(0o700)
    monkeypatch.setattr(supervisor, "available_gib", lambda: 20)
    monkeypatch.setattr(supervisor, "competing_codex", lambda path: [])
    assert supervisor.tick(state_file, fake) == "completed"
    state = supervisor.load_json(state_file)
    assert state["runs"] == 1
    assert state["session_id"] == "12345678-1234-1234-1234-123456789abc"
    assert supervisor.tick(state_file, fake) == "completed"
    assert json.loads((state_file.parent / "codex.jsonl").read_text().splitlines()[0])["type"] == "thread.started"


def test_prompt_change_parks_before_launch(tmp_path: Path, monkeypatch):
    _, state_file, _ = prepared(tmp_path)
    (state_file.parent / "prompt.txt").write_text("changed")
    monkeypatch.setattr(supervisor, "available_gib", lambda: 20)
    monkeypatch.setattr(supervisor, "competing_codex", lambda path: [])
    assert supervisor.tick(state_file, Path("/no/codex")) == "parked"
    assert supervisor.load_json(state_file)["runs"] == 0


def test_resume_uses_recorded_session_once(tmp_path: Path, monkeypatch):
    _, state_file, _ = prepared(tmp_path)
    state = supervisor.load_json(state_file)
    state.update(phase="running", runs=1, pid=100, start_ticks=7,
                 session_id="12345678-1234-1234-1234-123456789abc")
    supervisor.atomic_json(state_file, state)
    fake = tmp_path / "fake_codex"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "assert sys.argv[1:4] == ['exec', 'resume', '--json']\n"
        "assert sys.argv[4] == '12345678-1234-1234-1234-123456789abc'\n"
        "assert 'unfinished work only' in sys.stdin.read()\n"
        "print(json.dumps({'type':'turn.completed'}), flush=True)\n"
    )
    fake.chmod(0o700)
    monkeypatch.setattr(supervisor, "same_child", lambda value: False)
    monkeypatch.setattr(supervisor, "available_gib", lambda: 20)
    monkeypatch.setattr(supervisor, "competing_codex", lambda path: [])
    assert supervisor.tick(state_file, fake) == "completed"
    assert supervisor.load_json(state_file)["runs"] == 2
    assert supervisor.tick(state_file, fake) == "completed"


def test_competing_codex_detects_live_process_in_checkout(tmp_path: Path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    fake = tmp_path / "codex"
    fake.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(5)\n")
    fake.chmod(0o700)
    process = subprocess.Popen([str(fake)], cwd=checkout)
    try:
        for _ in range(20):
            if process.pid in supervisor.competing_codex(checkout):
                break
            time.sleep(0.05)
        assert process.pid in supervisor.competing_codex(checkout)
    finally:
        process.terminate()
        process.wait(timeout=2)
