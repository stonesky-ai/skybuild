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


WORKER = Path(__file__).resolve().parents[1] / "marshall_dunsel.py"
LOG_PATH = Path("/tmp/marshall_dunsel.log")
EXIT_PATH = Path("/tmp/marshall_dunsel.off-now")
DISABLED_PATH = Path("/tmp/marshall_dunsel.disabled")
PID_PATH = Path("/tmp/marshall_dunsel.pid")
CONTROL_LOCK = Path("/tmp/marshall_dunsel.control.lock")
_LOOPBACKS = {"127.0.0.1", "::1", "localhost"}


class MarshallConflict(Exception):
    """A fixed marshall action cannot run in current state."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@contextmanager
def _locked_control():
    descriptor = os.open(CONTROL_LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.chmod(CONTROL_LOCK, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _worker_processes() -> list[dict[str, int | str]]:
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
    temporary = PID_PATH.with_name(PID_PATH.name + ".web.tmp")
    temporary.write_text(f"{pid}\n", encoding="ascii")
    os.chmod(temporary, 0o600)
    temporary.replace(PID_PATH)


def set_enabled(enabled: bool) -> None:
    with _locked_control():
        if enabled:
            DISABLED_PATH.unlink(missing_ok=True)
        else:
            DISABLED_PATH.touch(mode=0o600, exist_ok=True)
            os.chmod(DISABLED_PATH, 0o600)


def start() -> dict:
    with _locked_control():
        if DISABLED_PATH.exists():
            raise MarshallConflict("Dunsel is disabled. Enable it before starting.")
        existing = _worker_processes()
        if existing:
            return {"started": False, "pid": existing[0]["pid"]}
        EXIT_PATH.unlink(missing_ok=True)
        nohup = shutil.which("nohup")
        if not nohup:
            raise RuntimeError("nohup is unavailable")
        LOG_PATH.touch(mode=0o600, exist_ok=True)
        os.chmod(LOG_PATH, 0o600)
        with LOG_PATH.open("a", encoding="utf-8") as output:
            process = subprocess.Popen(
                [nohup, sys.executable, str(WORKER), "--instance", "dunsel"],
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                cwd=str(WORKER.parent.parent),
                close_fds=True,
                start_new_session=True,
            )
        _write_pid(process.pid)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if any(int(row["pid"]) == process.pid for row in _worker_processes()):
                return {"started": True, "pid": process.pid}
            if process.poll() is not None:
                PID_PATH.unlink(missing_ok=True)
                raise RuntimeError("Dunsel exited during startup")
            time.sleep(0.02)
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        PID_PATH.unlink(missing_ok=True)
        raise RuntimeError("Dunsel did not become visible in the process table")


def graceful_stop() -> dict:
    with _locked_control():
        processes = _worker_processes()
        if not processes:
            return {"requested": False, "reason": "not-running"}
        EXIT_PATH.write_text(f"requested_at={_now()}\n", encoding="ascii")
        os.chmod(EXIT_PATH, 0o600)
        return {"requested": True, "pid": processes[0]["pid"]}


def kill() -> dict:
    with _locked_control():
        processes = _worker_processes()
        if not processes:
            return {"killed": False, "reason": "not-running"}
        pkill = shutil.which("pkill")
        if not pkill:
            raise RuntimeError("pkill is unavailable")
        pattern = "^" + re.escape(sys.executable) + " " + re.escape(str(WORKER)) + r" --instance dunsel$"
        result = subprocess.run([pkill, "-f", "--", pattern], capture_output=True, timeout=5, check=False)
        if result.returncode not in (0, 1):
            raise RuntimeError("pkill failed for the Dunsel process pattern")
        PID_PATH.unlink(missing_ok=True)
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
        stat = LOG_PATH.stat()
        last_seen = datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        tail = LOG_PATH.read_bytes()[-8192:].decode("utf-8", "replace").splitlines()
        return last_seen, tail[-1][:2000] if tail else None
    except OSError:
        return None, None


def snapshot() -> dict:
    processes = _worker_processes()
    pid = int(processes[0]["pid"]) if processes else None
    if pid is None:
        PID_PATH.unlink(missing_ok=True)
    last_seen, last_line = _log_snapshot()
    return {
        "marshall": "dunsel",
        "enabled": not DISABLED_PATH.exists(),
        "running": pid is not None,
        "pid": pid,
        "processes": _process_tree(pid),
        "top_line": _top_line(pid),
        "last_seen": last_seen,
        "last_line": last_line,
        "exit_requested": EXIT_PATH.exists(),
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
