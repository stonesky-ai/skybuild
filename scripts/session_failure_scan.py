"""Summarize recent Codex tool failures without copying log contents."""

import argparse
import collections
import json
import signal
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


PATTERNS = {
    "test_failure": ("FAILED ", "AssertionError", "pytest.fail"),
    "syntax_or_import": ("SyntaxError", "ImportError", "ModuleNotFoundError"),
    "missing_tool_or_path": ("No such file or directory", "command not found", "exit_code\":127"),
    "permission_or_auth": ("Permission denied", "Bad owner or permissions", "authentication failed"),
    "timeout_or_hang": ("timed out", "TimeoutExpired", "exit_code\":124"),
    "patch_or_command": ("apply_patch verification failed", "Script error", "Script failed"),
}


class _WatchStopped(Exception):
    """The finite watch ended while a scan was in progress."""


def _checkpoint(stopped):
    if stopped is not None and stopped():
        raise _WatchStopped


def scan(log_root: Path, minutes: int, *, stopped=None, deadline=None) -> dict:
    _checkpoint(stopped)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    # A long-running session stays under its creation date while receiving new events.
    candidates = []
    visited = 0
    inventory_deadline = time.monotonic() + 5
    for path in log_root.rglob("*.jsonl"):
        _checkpoint(stopped)
        visited += 1
        if visited > 10_000 or time.monotonic() > inventory_deadline:
            raise RuntimeError("Session log inventory exceeded its bound")
        if path.stat().st_mtime >= cutoff.timestamp():
            candidates.append(path)
    candidates.sort()
    if len(candidates) > 200:
        raise RuntimeError("Too many recent session logs for one bounded scan")
    if not candidates:
        return {"window_minutes": minutes, "sessions_scanned": 0, "categories": {},
                "agents_by_category": {}, "note": "No recent session logs found"}
    _checkpoint(stopped)
    timeout = 20 if deadline is None else min(20, max(0.001, deadline - time.monotonic()))
    grep = subprocess.run(
        ["rg", "-l", "--max-filesize", "50M", "Script error|Script failed|FAILED |AssertionError|SyntaxError|"
         "ImportError|ModuleNotFoundError|No such file or directory|Permission denied|"
         "Bad owner or permissions|timed out|TimeoutExpired|apply_patch verification failed|exit_code",
         *map(str, candidates)], capture_output=True, text=True, check=False, timeout=timeout,
    )
    _checkpoint(stopped)
    if grep.returncode not in {0, 1}:
        raise RuntimeError("ripgrep failed to read session logs")
    counts = collections.Counter()
    sessions = set()
    agents_by_category = collections.defaultdict(set)
    for name in grep.stdout.splitlines():
        _checkpoint(stopped)
        path = Path(name)
        if path.stat().st_mtime < cutoff.timestamp():
            continue
        agent = "unknown"
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                _checkpoint(stopped)
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("type") == "session_meta":
                    agent = event.get("payload", {}).get("agent_path", "unknown")
                    continue
                when = event.get("timestamp", "")
                if when and datetime.fromisoformat(when.replace("Z", "+00:00")) < cutoff:
                    continue
                if event.get("type") != "response_item":
                    continue
                item = event.get("payload", {})
                if item.get("type") not in {"custom_tool_call_output", "function_call_output"}:
                    continue
                blocks = item.get("output", [])
                if isinstance(blocks, str):
                    blocks = [{"text": blocks}]
                for block in blocks if isinstance(blocks, list) else []:
                    raw = block.get("text", "") if isinstance(block, dict) else ""
                    if not isinstance(raw, str):
                        continue
                    try:
                        result = json.loads(raw)
                    except ValueError:
                        result = None
                    if isinstance(result, dict) and "exit_code" in result:
                        if type(result["exit_code"]) is not int or result["exit_code"] == 0:
                            continue
                        output = str(result.get("output", ""))
                    elif raw.startswith(("Script error", "Script failed")):
                        output = raw
                    else:
                        continue
                    matched = [category for category, words in PATTERNS.items() if any(word in output for word in words)]
                    if not matched:
                        matched = ["other_nonzero_exit"]
                    sessions.add(agent)
                    counts.update(matched)
                    for category in matched:
                        agents_by_category[category].add(agent)
    return {"window_minutes": minutes, "sessions_scanned": len(sessions),
            "categories": dict(sorted(counts.items())),
            "agents_by_category": {key: sorted(value) for key, value in sorted(agents_by_category.items())},
            "note": "Candidate failures only; inspect relevant tool results before creating a prevention skill."}


def watch(log_root: Path, minutes: int, seconds: float, cancelled: threading.Event, *, deadline=None) -> None:
    """Use a fixed monotonic cutoff; wall-clock rollback cannot extend the watch."""
    if type(seconds) not in (int, float) or not 0 <= seconds <= 28800:
        raise ValueError("Watch lifetime must be finite and at most eight hours")
    if deadline is None:
        deadline = time.monotonic() + seconds
    elif type(deadline) not in (int, float) or not -float("inf") < deadline < float("inf"):
        raise ValueError("Watch cutoff must be finite")
    elif deadline > time.monotonic() + seconds:
        raise ValueError("Watch cutoff exceeds the bounded lifetime")

    def stopped():
        return cancelled.is_set() or time.monotonic() >= deadline

    while not stopped():
        try:
            result = scan(log_root, minutes, stopped=stopped, deadline=deadline)
        except _WatchStopped:
            break
        except subprocess.TimeoutExpired:
            if stopped():
                break
            raise
        if stopped():
            break
        print(json.dumps(result, sort_keys=True), flush=True)
        cancelled.wait(min(1800, max(0, deadline - time.monotonic())))


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--logs-root", type=Path, default=Path.home() / ".codex/sessions")
    parser.add_argument("--minutes", type=int, default=30)
    parser.add_argument("--watch", action="store_true", help="Repeat every 30 minutes within an explicit finite bound")
    parser.add_argument("--duration-minutes", type=int, help="Watch lifetime, from 1 to 480 minutes")
    parser.add_argument("--approval-deadline", help="Watch cutoff as an ISO-8601 timestamp with timezone")
    args = parser.parse_args(argv)
    if not args.watch:
        if args.duration_minutes is not None or args.approval_deadline is not None:
            parser.error("Watch bounds require --watch")
        print(json.dumps(scan(args.logs_root, args.minutes), sort_keys=True), flush=True)
        return
    if args.duration_minutes is None and args.approval_deadline is None:
        parser.error("--watch requires --duration-minutes or --approval-deadline")
    if args.duration_minutes is not None and not 1 <= args.duration_minutes <= 480:
        parser.error("Watch duration must be from 1 to 480 minutes")
    # Pin the monotonic reference before sampling UTC or installing handlers.
    # Scheduling delays during setup consume the original lifetime.
    started = time.monotonic()
    seconds = (args.duration_minutes or 480) * 60
    if args.approval_deadline is not None:
        try:
            deadline = datetime.fromisoformat(args.approval_deadline.replace("Z", "+00:00"))
            if deadline.tzinfo is None:
                raise ValueError
        except ValueError:
            parser.error("Approval deadline requires an ISO-8601 timestamp with timezone")
        seconds = min(seconds, max(0, (deadline - datetime.now(timezone.utc)).total_seconds()))
    cutoff = started + seconds
    cancelled = threading.Event()
    previous = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
    try:
        for number in previous:
            signal.signal(number, lambda *_: cancelled.set())
        watch(args.logs_root, args.minutes, seconds, cancelled, deadline=cutoff)
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


if __name__ == "__main__":
    main()
