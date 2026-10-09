"""Small local CPU-only marshall controlled by the Workbench preview."""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

_SOURCE_ROOT = str(Path(__file__).resolve().parents[1])
if _SOURCE_ROOT not in sys.path:
    sys.path.insert(0, _SOURCE_ROOT)

from skybuild import dunsel_state  # noqa: E402



def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _memory() -> dict[str, int | None]:
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            key, _, raw = line.partition(":")
            if key in {"MemTotal", "MemAvailable"}:
                values[key] = int(raw.strip().split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    return {"memory_total_bytes": total, "memory_available_bytes": available,
            "memory_used_bytes": max(0, total - available) if total is not None and available is not None else None}


def _disk(path: str) -> dict[str, int | str]:
    try:
        usage = shutil.disk_usage(path)
        return {"path": path, "total_bytes": usage.total, "used_bytes": usage.used,
                "free_bytes": usage.free}
    except OSError as error:
        return {"path": path, "error": type(error).__name__}


def _write_pid() -> None:
    dunsel_state.write_process_identity(os.getpid())


def _clear_pid() -> None:
    try:
        identity = dunsel_state.process_identity()
        if identity and identity["pid"] == os.getpid():
            dunsel_state.unlink_file(dunsel_state.PID_FILE)
    except (OSError, UnicodeError, ValueError):
        pass


def _log(file, record: dict) -> None:
    file.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
    file.flush()


def run() -> int:
    if sys.argv[1:] != ["--instance", "dunsel"]:
        return 2
    _write_pid()
    try:
        log_descriptor = dunsel_state.open_file(
            dunsel_state.LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND
        )
        with os.fdopen(log_descriptor, "a", encoding="utf-8", buffering=1) as log:
            _log(log, {"at": _now(), "event": "started", "pid": os.getpid()})
            while True:
                if dunsel_state.file_exists(dunsel_state.EXIT_FILE):
                    dunsel_state.unlink_file(dunsel_state.EXIT_FILE)
                    exit_path = dunsel_state.STATE_DIR / dunsel_state.EXIT_FILE
                    _log(log, {"at": _now(), "event": "saw exit file", "path": str(exit_path)})
                    return 0

                _log(log, {
                    "at": _now(), "event": "sample", "pid": os.getpid(),
                    "memory": _memory(), "root_disk": _disk("/"), "tmp_disk": _disk("/tmp"),
                })
                now = datetime.now(UTC)
                seconds = now.second + now.microsecond / 1_000_000
                until_minute = max(0.05, 60.0 - seconds)
                time.sleep(min(10.0, until_minute))
    finally:
        _clear_pid()


if __name__ == "__main__":
    raise SystemExit(run())
