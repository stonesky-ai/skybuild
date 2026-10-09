"""Small local CPU-only marshall controlled by the Workbench preview."""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path


LOG_PATH = Path("/tmp/marshall_dunsel.log")
EXIT_PATH = Path("/tmp/marshall_dunsel.off-now")
PID_PATH = Path("/tmp/marshall_dunsel.pid")


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
    temporary = PID_PATH.with_suffix(".pid.tmp")
    temporary.write_text(f"{os.getpid()}\n", encoding="ascii")
    os.chmod(temporary, 0o600)
    temporary.replace(PID_PATH)


def _clear_pid() -> None:
    try:
        if PID_PATH.read_text(encoding="ascii").strip() == str(os.getpid()):
            PID_PATH.unlink()
    except (OSError, UnicodeError):
        pass


def _log(file, record: dict) -> None:
    file.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
    file.flush()


def run() -> int:
    if sys.argv[1:] != ["--instance", "dunsel"]:
        return 2
    LOG_PATH.touch(mode=0o600, exist_ok=True)
    os.chmod(LOG_PATH, 0o600)
    _write_pid()
    try:
        with LOG_PATH.open("a", encoding="utf-8", buffering=1) as log:
            _log(log, {"at": _now(), "event": "started", "pid": os.getpid()})
            while True:
                if EXIT_PATH.exists():
                    try:
                        EXIT_PATH.unlink()
                    except FileNotFoundError:
                        pass
                    _log(log, {"at": _now(), "event": "saw exit file", "path": str(EXIT_PATH)})
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
