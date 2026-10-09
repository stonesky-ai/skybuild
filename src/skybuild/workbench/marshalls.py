"""Fixed local process controls for development Workbench marshalls."""
from __future__ import annotations

import fcntl
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from .. import dunsel_state


WORKER = Path(__file__).resolve().parents[1] / "marshall_dunsel.py"
_LOOPBACKS = {"127.0.0.1", "::1", "localhost"}


class MarshallConflict(Exception):
    """A fixed marshall action cannot run in current state."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@contextmanager
def _locked_control():
    descriptor = dunsel_state.open_file(
        dunsel_state.LOCK_FILE, os.O_CREAT | os.O_RDWR
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _worker_processes() -> list[dict[str, int | str]]:
    identity = dunsel_state.process_identity()
    if identity is not None:
        return [identity]
    matches: list[dict[str, int | str]] = []
    try:
        entries = Path("/proc").iterdir()
    except OSError:
        return matches
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            args = (entry / "cmdline").read_bytes().split(b"\0")
            args = [part.decode("utf-8", "replace") for part in args if part]
        except OSError:
            continue
        if len(args) == 4 and args[0] == sys.executable and args[1] == str(WORKER) and args[2:] == ["--instance", "dunsel"]:
            matches.append({"pid": int(entry.name), "command": " ".join(args)})
    return sorted(matches, key=lambda row: int(row["pid"]))


def _write_pid(pid: int) -> None:
    dunsel_state.write_process_identity(pid)


def set_enabled(enabled: bool) -> None:
    with _locked_control():
        if enabled:
            dunsel_state.unlink_file(dunsel_state.DISABLED_FILE)
        else:
            dunsel_state.touch_file(dunsel_state.DISABLED_FILE)


def start() -> dict:
    with _locked_control():
        if dunsel_state.file_exists(dunsel_state.DISABLED_FILE):
            raise MarshallConflict("Dunsel is disabled. Enable it before starting.")
        existing = _worker_processes()
        if existing:
            return {"started": False, "pid": existing[0]["pid"]}
        dunsel_state.unlink_file(dunsel_state.EXIT_FILE)
        nohup = shutil.which("nohup")
        if not nohup:
            raise RuntimeError("nohup is unavailable")
        log_descriptor = dunsel_state.open_file(
            dunsel_state.LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND
        )
        with os.fdopen(log_descriptor, "a", encoding="utf-8") as output:
            process = subprocess.Popen(
                [nohup, sys.executable, str(WORKER), "--instance", "dunsel"],
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                cwd=str(WORKER.parent.parent),
                close_fds=True,
                start_new_session=True,
            )
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if any(int(row["pid"]) == process.pid for row in _worker_processes()):
                _write_pid(process.pid)
                return {"started": True, "pid": process.pid}
            if process.poll() is not None:
                dunsel_state.unlink_file(dunsel_state.PID_FILE)
                raise RuntimeError("Dunsel exited during startup")
            time.sleep(0.02)
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        dunsel_state.unlink_file(dunsel_state.PID_FILE)
        raise RuntimeError("Dunsel did not become visible in the process table")


def graceful_stop() -> dict:
    with _locked_control():
        processes = _worker_processes()
        if not processes:
            return {"requested": False, "reason": "not-running"}
        dunsel_state.write_text(
            dunsel_state.EXIT_FILE, f"requested_at={_now()}\n"
        )
        return {"requested": True, "pid": processes[0]["pid"]}


def kill() -> dict:
    with _locked_control():
        processes = _worker_processes()
        if not processes:
            return {"killed": False, "reason": "not-running"}
        pkill = shutil.which("pkill")
        if not pkill:
            raise RuntimeError("pkill is unavailable")
        args = processes[0].get("args", [sys.executable, str(WORKER), "--instance", "dunsel"])
        pattern = "^" + " ".join(re.escape(arg) for arg in args) + "$"
        result = subprocess.run([pkill, "-f", "--", pattern], capture_output=True, timeout=5, check=False)
        if result.returncode not in (0, 1):
            raise RuntimeError("pkill failed for the Dunsel process pattern")
        dunsel_state.unlink_file(dunsel_state.PID_FILE)
        return {"killed": result.returncode == 0, "pids": [row["pid"] for row in processes]}


def _proc_row(pid: int) -> dict | None:
    root = Path("/proc") / str(pid)
    try:
        stat_text = (root / "stat").read_text(encoding="ascii")
        rest = stat_text[stat_text.rfind(")") + 2:].split()
        parent = int(rest[1])
        state = rest[0]
        command = (root / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace").strip()
    except (OSError, ValueError, IndexError):
        return None
    return {"pid": pid, "ppid": parent, "state": state, "command": command[:1200]}


def _process_tree(root_pid: int | None) -> list[dict]:
    if root_pid is None:
        return []
    rows: dict[int, dict] = {}
    try:
        entries = Path("/proc").iterdir()
    except OSError:
        return []
    for entry in entries:
        if entry.name.isdigit():
            row = _proc_row(int(entry.name))
            if row:
                rows[row["pid"]] = row
    selected = {root_pid} if root_pid in rows else set()
    pending = [root_pid]
    while pending:
        parent = pending.pop()
        for pid, row in rows.items():
            if row["ppid"] == parent and pid not in selected:
                selected.add(pid)
                pending.append(pid)
    return [rows[pid] for pid in sorted(selected)]


def _top_line(pid: int | None) -> str | None:
    top = shutil.which("top")
    if not top or pid is None:
        return None
    try:
        result = subprocess.run([top, "-b", "-n", "1", "-p", str(pid)],
                                capture_output=True, text=True, timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in result.stdout.splitlines():
        fields = line.split()
        if fields and fields[0] == str(pid):
            return line.strip()[:1200]
    return None


def _log_snapshot() -> tuple[str | None, str | None]:
    try:
        info, raw = dunsel_state.read_tail(dunsel_state.LOG_FILE, 8192)
        last_seen = datetime.fromtimestamp(info.st_mtime, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        tail = raw.decode("utf-8", "replace").splitlines()
        return last_seen, tail[-1][:2000] if tail else None
    except OSError:
        return None, None


def snapshot() -> dict:
    processes = _worker_processes()
    pid = int(processes[0]["pid"]) if processes else None
    last_seen, last_line = _log_snapshot()
    return {
        "marshall": "dunsel",
        "enabled": not dunsel_state.file_exists(dunsel_state.DISABLED_FILE),
        "running": pid is not None,
        "pid": pid,
        "processes": _process_tree(pid),
        "top_line": _top_line(pid),
        "last_seen": last_seen,
        "last_line": last_line,
        "exit_requested": dunsel_state.file_exists(dunsel_state.EXIT_FILE),
        "observed_at": _now(),
    }


def require_local_request(request, *, mutation: bool = False) -> None:
    client = getattr(request, "client", None)
    if client is None or client.host not in _LOOPBACKS:
        raise PermissionError("Dunsel controls are local-preview only")
    host = urlsplit("//" + request.headers.get("host", "")).hostname
    if host not in _LOOPBACKS:
        raise PermissionError("Dunsel controls are local-preview only")
    if mutation:
        origin = urlsplit(request.headers.get("origin", ""))
        request_host = urlsplit("//" + request.headers.get("host", ""))
        if (origin.scheme != request.url.scheme or origin.hostname not in _LOOPBACKS
                or origin.hostname != request_host.hostname or origin.port != request_host.port):
            raise PermissionError("Dunsel changes require a same-origin local request")
