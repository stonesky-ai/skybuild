"""Only completed failing tool results count as session failures."""

import importlib.util
import json
import select
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts/session_failure_scan.py"
spec = importlib.util.spec_from_file_location("skybuild_session_failure_scan", SCRIPT)
scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner)


def event(exit_code, output):
    return {"timestamp": datetime.now(timezone.utc).isoformat(), "type": "response_item",
            "payload": {"type": "custom_tool_call_output", "output": [
                {"type": "input_text", "text": json.dumps({"exit_code": exit_code, "output": output})}]}}


def test_scan_ignores_running_and_successful_quoted_failures(tmp_path):
    today = datetime.now(timezone.utc)
    folder = tmp_path / f"{today:%Y/%m/%d}"
    folder.mkdir(parents=True)
    path = folder / "rollout-test.jsonl"
    rows = [
        {"type": "session_meta", "payload": {"agent_path": "/root/test"}},
        event(None, "still running"),
        event(0, "quoted FAILED text in a passing tool result"),
        event(1, "FAILED tests/test_example.py::test_real - AssertionError"),
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    result = scanner.scan(tmp_path, 30)
    assert result["categories"] == {"test_failure": 1}
    assert result["agents_by_category"] == {"test_failure": ["/root/test"]}


def test_empty_recent_window_has_no_failures(tmp_path):
    assert scanner.scan(tmp_path, 30)["categories"] == {}


def test_long_session_in_old_date_folder_is_selected_by_modification_time(tmp_path):
    folder = tmp_path / "2026/01/01"
    folder.mkdir(parents=True)
    path = folder / "rollout-long-running.jsonl"
    path.write_text(json.dumps(event(1, "FAILED old-session-test - AssertionError")) + "\n")
    assert scanner.scan(tmp_path, 30)["categories"] == {"test_failure": 1}


@pytest.mark.parametrize("arguments", [
    ["--watch"], ["--watch", "--duration-minutes", "0"],
    ["--watch", "--duration-minutes", "481"],
    ["--watch", "--approval-deadline", "2026-10-09T15:20:53"],
    ["--duration-minutes", "1"],
])
def test_invalid_watch_bound_never_scans(monkeypatch, arguments):
    monkeypatch.setattr(scanner, "scan", lambda *_args, **_kwargs: pytest.fail("must not scan"))
    with pytest.raises(SystemExit) as caught:
        scanner.main(arguments)
    assert caught.value.code == 2


def test_cli_unbounded_watch_exits_instead_of_waiting(tmp_path):
    result = subprocess.run([sys.executable, str(SCRIPT), "--watch", "--logs-root", str(tmp_path)],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 2 and result.stdout == ""
    assert "requires --duration-minutes or --approval-deadline" in result.stderr


def test_earliest_bound_and_eight_hour_maximum(monkeypatch):
    now = datetime(2026, 10, 9, 10, tzinfo=timezone.utc)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(scanner, "datetime", Clock)
    durations = []
    monkeypatch.setattr(scanner, "watch", lambda _root, _minutes, seconds, _event, **_kwargs: durations.append(seconds))
    scanner.main(["--watch", "--duration-minutes", "60", "--approval-deadline",
                  (now + timedelta(minutes=10)).isoformat()])
    scanner.main(["--watch", "--duration-minutes", "5", "--approval-deadline",
                  (now + timedelta(minutes=10)).isoformat()])
    scanner.main(["--watch", "--approval-deadline", (now + timedelta(days=1)).isoformat()])
    assert durations == [600, 300, 28800]


def test_expired_deadline_runs_no_scan(monkeypatch):
    monkeypatch.setattr(scanner, "scan", lambda *_args, **_kwargs: pytest.fail("expired watch must not scan"))
    scanner.main(["--watch", "--approval-deadline", "2000-01-01T00:00:00Z"])


@pytest.mark.parametrize("seconds", [float("inf"), float("nan"), -1, 28801])
def test_direct_watch_refuses_unbounded_lifetime(tmp_path, seconds):
    with pytest.raises(ValueError, match="finite"):
        scanner.watch(tmp_path, 30, seconds, threading.Event())


def test_wall_clock_rollback_cannot_extend_watch(monkeypatch, tmp_path, capsys):
    ticks = [0.0]
    wall = [datetime(2026, 10, 9, 10, tzinfo=timezone.utc)]
    start = wall[0]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return wall[0]

    class Event:
        def is_set(self):
            return False

        def wait(self, seconds):
            ticks[0] += seconds
            wall[0] -= timedelta(hours=1)

    calls = []
    monkeypatch.setattr(scanner, "datetime", Clock)
    monkeypatch.setattr(scanner.time, "monotonic", lambda: ticks[0])
    monkeypatch.setattr(scanner.threading, "Event", Event)
    monkeypatch.setattr(scanner, "scan", lambda *_args, **_kwargs: calls.append(ticks[0]) or {})
    scanner.main(["--watch", "--logs-root", str(tmp_path), "--approval-deadline",
                  (start + timedelta(seconds=1900)).isoformat()])
    assert calls == [0, 1800]
    assert ticks[0] == 1900
    assert len(capsys.readouterr().out.splitlines()) == 2


def test_delayed_watch_entry_and_rollback_cannot_renew_cutoff(monkeypatch, tmp_path, capsys):
    ticks = [0.0]
    wall = [datetime(2026, 10, 9, 10, tzinfo=timezone.utc)]
    approval = wall[0] + timedelta(seconds=120)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            # Even delay between the monotonic reference and UTC sampling must
            # not extend the original authority lifetime.
            ticks[0] += 10
            return wall[0]

    class Event:
        def __init__(self):
            ticks[0] += 90  # Signal/cancellation setup before entering watch.
            wall[0] -= timedelta(hours=1)

        def is_set(self):
            return False

        def wait(self, seconds):
            ticks[0] += seconds

    calls = []
    monkeypatch.setattr(scanner, "datetime", Clock)
    monkeypatch.setattr(scanner.time, "monotonic", lambda: ticks[0])
    monkeypatch.setattr(scanner.threading, "Event", Event)
    monkeypatch.setattr(scanner, "scan", lambda *_args, **_kwargs: calls.append(ticks[0]) or {})
    scanner.main(["--watch", "--logs-root", str(tmp_path), "--approval-deadline", approval.isoformat()])
    assert calls == [100]
    assert ticks[0] == 120  # Only the 20 remaining seconds are available.
    assert len(capsys.readouterr().out.splitlines()) == 1


def test_sigterm_wakes_owned_watch_without_another_scan(tmp_path):
    process = subprocess.Popen([sys.executable, str(SCRIPT), "--watch", "--duration-minutes", "1",
                                "--logs-root", str(tmp_path)], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    try:
        assert select.select([process.stdout], [], [], 5)[0], "watch did not emit its first scan"
        assert json.loads(process.stdout.readline())["categories"] == {}
        process.terminate()
        output, errors = process.communicate(timeout=5)
        assert process.returncode == 0 and output == "" and errors == ""
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


def test_cancel_during_parsing_discards_partial_scan(monkeypatch, tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_text(json.dumps(event(1, "FAILED first")) + "\n" + json.dumps(event(1, "FAILED second")) + "\n")
    cancelled = threading.Event()
    loads = scanner.json.loads
    parsed = []

    def cancel_after_record(raw):
        value = loads(raw)
        parsed.append(value)
        cancelled.set()
        return value

    monkeypatch.setattr(scanner.json, "loads", cancel_after_record)
    with pytest.raises(scanner._WatchStopped):
        scanner.scan(tmp_path, 30, stopped=cancelled.is_set)
    assert len(parsed) == 2  # The first event and its nested tool output only.


def test_ripgrep_timeout_uses_remaining_watch_budget(monkeypatch, tmp_path):
    (tmp_path / "session.jsonl").write_text("{}\n")
    ticks = [0.0]
    monkeypatch.setattr(scanner.time, "monotonic", lambda: ticks[0])
    original_stat = Path.stat

    def inventory_delay(path, *args, **kwargs):
        ticks[0] = 4
        return original_stat(path, *args, **kwargs)

    def timed_out(*args, **kwargs):
        assert kwargs["timeout"] == 1
        ticks[0] = 5
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(Path, "stat", inventory_delay)
    monkeypatch.setattr(scanner.subprocess, "run", timed_out)
    scanner.watch(tmp_path, 30, 5, threading.Event())
