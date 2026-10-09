"""Summarize recent Codex tool failures without copying log contents."""

import argparse
import collections
import json
import subprocess
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


def scan(log_root: Path, minutes: int) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    current_day = datetime.now(timezone.utc).date()
    days = (current_day, cutoff.date())
    candidates = sorted({path for day in days for path in
                         (log_root / f"{day:%Y/%m/%d}").glob("*.jsonl")
                         if path.stat().st_mtime >= cutoff.timestamp()})
    if len(candidates) > 200:
        raise RuntimeError("Too many recent session logs for one bounded scan")
    if not candidates:
        return {"window_minutes": minutes, "sessions_scanned": 0, "categories": {},
                "agents_by_category": {}, "note": "No recent session logs found"}
    grep = subprocess.run(
        ["rg", "-l", "--max-filesize", "50M", "Script error|Script failed|FAILED |AssertionError|SyntaxError|"
         "ImportError|ModuleNotFoundError|No such file or directory|Permission denied|"
         "Bad owner or permissions|timed out|TimeoutExpired|apply_patch verification failed|exit_code",
         *map(str, candidates)], capture_output=True, text=True, check=False, timeout=20,
    )
    if grep.returncode not in {0, 1}:
        raise RuntimeError("ripgrep failed to read session logs")
    counts = collections.Counter()
    sessions = set()
    agents_by_category = collections.defaultdict(set)
    for name in grep.stdout.splitlines():
        path = Path(name)
        if path.stat().st_mtime < cutoff.timestamp():
            continue
        agent = "unknown"
        with path.open(encoding="utf-8") as stream:
            for line in stream:
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--logs-root", type=Path, default=Path.home() / ".codex/sessions")
    parser.add_argument("--minutes", type=int, default=30)
    parser.add_argument("--watch", action="store_true", help="repeat every 30 minutes while this process runs")
    args = parser.parse_args()
    while True:
        print(json.dumps(scan(args.logs_root, args.minutes), sort_keys=True), flush=True)
        if not args.watch:
            break
        time.sleep(1800)


if __name__ == "__main__":
    main()
