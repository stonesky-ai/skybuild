"""Only completed failing tool results count as session failures."""

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path


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
