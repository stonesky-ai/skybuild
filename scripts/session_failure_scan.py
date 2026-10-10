"""Summarize recent Codex tool failures without copying log contents."""

import argparse
import collections
import json
import re
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

# Only fixed labels escape the log reader; arguments and error text stay private.
DIAGNOSTICS = {
    "missing_path": ("No such file or directory",),
    "missing_command": ("command not found",),
    "readonly_cache": ("Could not acquire lock", "Read-only file system"),
    "python_syntax": ("SyntaxError",),
    "python_import": ("ImportError", "ModuleNotFoundError"),
    "javascript_syntax": ("Unexpected token", "Unexpected identifier"),
    "patch_context": ("apply_patch verification failed", "Failed to find expected lines"),
    "unknown_option": ("unrecognized arguments", "unexpected argument", "unknown flag", "unknown option"),
    "test_failure": ("FAILED ", "AssertionError"),
    "timeout": ("timed out", "TimeoutExpired"),
}


def _operation(item):
    """Classify an invocation without retaining its arguments or arbitrary labels."""
    value = item.get("input", item.get("arguments", ""))
    if not isinstance(value, str):
        return "other"
    for label, pattern in (("test", r"\bpytest\b"), ("patch", r"\bapply_patch\b"),
                           ("navigation", r"\b(?:rg|grep|sed|cat|ls|codegraph)\b"),
                           ("git", r"\bgit\b"), ("python", r"\b(?:python3?|project_python)\b")):
        if re.search(pattern, value):
            return label
    return "other"


class _WatchStopped(Exception):
    """The finite watch ended while a scan was in progress."""


def _checkpoint(stopped):
    if stopped is not None and stopped():
        raise _WatchStopped


def scan(log_root: Path, minutes: int, *, stopped=None, deadline=None, details=False) -> dict:
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
        summary = {"window_minutes": minutes, "sessions_scanned": 0, "categories": {},
                   "agents_by_category": {}, "note": "No recent session logs found"}
        if details:
            summary["details"] = {"failed_results": 0, "paired_results": 0, "exit_counts": {},
                                  "signatures": {}, "operations": {}, "examples": {}}
        return summary
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
    exit_counts = collections.Counter()
    signatures = collections.Counter()
    operations = collections.Counter()
    examples = collections.defaultdict(list)
    paired = 0
    for name in grep.stdout.splitlines():
        _checkpoint(stopped)
        path = Path(name)
        if path.stat().st_mtime < cutoff.timestamp():
            continue
        agent = "unknown"
        calls = {}
        with path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                _checkpoint(stopped)
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("type") == "session_meta":
                    agent = event.get("payload", {}).get("agent_path", "unknown")
                    continue
                if event.get("type") != "response_item":
                    continue
                item = event.get("payload", {})
                call_id = item.get("call_id")
                if details and item.get("type") in {"custom_tool_call", "function_call"}:
                    if isinstance(call_id, str):
                        calls[call_id] = _operation(item)
                    continue
                when = event.get("timestamp", "")
                if when and datetime.fromisoformat(when.replace("Z", "+00:00")) < cutoff:
                    continue
                if item.get("type") not in {"custom_tool_call_output", "function_call_output"}:
                    continue
                operation = calls.pop(call_id, None) if isinstance(call_id, str) else None
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
                        exit_code = str(result["exit_code"])
                    elif raw.startswith(("Script error", "Script failed")):
                        output = raw
                        exit_code = "script_error"
                    else:
                        continue
                    matched = [category for category, words in PATTERNS.items() if any(word in output for word in words)]
                    if not matched:
                        matched = ["other_nonzero_exit"]
                    sessions.add(agent)
                    counts.update(matched)
                    for category in matched:
                        agents_by_category[category].add(agent)
                    if details:
                        exit_counts[exit_code] += 1
                        operations[operation or "unpaired"] += 1
                        paired += operation is not None
                        labels = [key for key, words in DIAGNOSTICS.items()
                                  if any(word in output for word in words)] or ["unclassified"]
                        signatures.update(labels)
                        for label in labels:
                            if len(examples[label]) < 3:
                                examples[label].append({"session": path.name, "line": line_number,
                                                        "exit": exit_code, "operation": operation or "unpaired"})
    summary = {"window_minutes": minutes, "sessions_scanned": len(sessions),
            "categories": dict(sorted(counts.items())),
            "agents_by_category": {key: sorted(value) for key, value in sorted(agents_by_category.items())},
            "note": "Candidate failures only; inspect relevant tool results before creating a prevention skill."}
    if details:
        summary["details"] = {"failed_results": sum(exit_counts.values()), "paired_results": paired,
                              "exit_counts": dict(sorted(exit_counts.items())),
                              "signatures": dict(sorted(signatures.items())),
                              "operations": dict(sorted(operations.items())),
                              "examples": dict(sorted(examples.items()))}
    return summary


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
    parser.add_argument("--details", action="store_true", help="Include safe exit/signature counts and paired event locations")
    parser.add_argument("--watch", action="store_true", help="Repeat every 30 minutes within an explicit finite bound")
    parser.add_argument("--duration-minutes", type=int, help="Watch lifetime, from 1 to 480 minutes")
    parser.add_argument("--approval-deadline", help="Watch cutoff as an ISO-8601 timestamp with timezone")
    args = parser.parse_args(argv)
    if not args.watch:
        if args.duration_minutes is not None or args.approval_deadline is not None:
            parser.error("Watch bounds require --watch")
        print(json.dumps(scan(args.logs_root, args.minutes, details=args.details), sort_keys=True), flush=True)
        return
    if args.details:
        parser.error("--details is for a single scan, not watch mode")
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
