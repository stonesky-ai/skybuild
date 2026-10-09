"""Private, no-follow state files for the local Dunsel preview utility."""
from __future__ import annotations

import fcntl
import json
import os
import stat
from pathlib import Path
from contextlib import contextmanager


STATE_DIR = Path.home() / ".local" / "state" / "skybuild" / "dunsel"
LOG_FILE = "dunsel.log"
EXIT_FILE = "dunsel.off-now"
DISABLED_FILE = "dunsel.disabled"
PID_FILE = "dunsel.pid"
LOCK_FILE = "dunsel.control.lock"
STARTUP_FILE = "dunsel.startup"
INTENT_FILE = "dunsel.launch-intent"
PID_PENDING_FILE = "dunsel.pid.pending"
_STATE_FILES = {LOG_FILE, EXIT_FILE, DISABLED_FILE, PID_FILE, LOCK_FILE, STARTUP_FILE, INTENT_FILE, PID_PENDING_FILE}
_NOFOLLOW = os.O_NOFOLLOW
_NONBLOCK = os.O_NONBLOCK
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


def _open_state_dir() -> int:
    """Open or create state path one directory at a time, refusing symlinks."""
    path = Path(STATE_DIR)
    if not path.is_absolute():
        raise ValueError("Dunsel state directory must be absolute")
    names = path.parts[1:]
    if not names or any(name in {"", ".", ".."} for name in names):
        raise ValueError("Dunsel state directory path is invalid")

    flags = os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW | _CLOEXEC
    descriptor = os.open(path.anchor, flags)
    uid = os.getuid()
    try:
        for index, name in enumerate(names):
            final = index == len(names) - 1
            try:
                os.mkdir(name, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(name, flags, dir_fd=descriptor)
            try:
                info = os.fstat(child)
                if not stat.S_ISDIR(info.st_mode):
                    raise PermissionError("Dunsel state path contains a non-directory")
                if final:
                    if info.st_uid != uid:
                        raise PermissionError("Dunsel state directory has a different owner")
                    os.fchmod(child, 0o700)
                elif not info.st_mode & stat.S_ISVTX and (
                    info.st_uid not in {0, uid} or info.st_mode & 0o022
                ):
                    raise PermissionError("Dunsel state parent is writable by another user")
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def open_file(name: str, flags: int, *, mode: int = 0o600) -> int:
    """Open one owned regular file without following links or truncating first."""
    if name not in _STATE_FILES:
        raise ValueError("unknown Dunsel state file")
    directory = _open_state_dir()
    try:
        descriptor = os.open(
            name, flags | _NOFOLLOW | _NONBLOCK | _CLOEXEC, mode, dir_fd=directory
        )
    finally:
        os.close(directory)

    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1):
            raise PermissionError("unsafe Dunsel state file")
        os.fchmod(descriptor, 0o600)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def file_exists(name: str) -> bool:
    try:
        descriptor = open_file(name, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        return False
    os.close(descriptor)
    return True


def unlink_file(name: str, *, missing_ok: bool = True) -> None:
    if name not in _STATE_FILES:
        raise ValueError("unknown Dunsel state file")
    directory = _open_state_dir()
    try:
        try:
            os.unlink(name, dir_fd=directory)
        except FileNotFoundError:
            if not missing_ok:
                raise
    finally:
        os.close(directory)


def touch_file(name: str) -> None:
    descriptor = open_file(name, os.O_WRONLY | os.O_CREAT)
    os.close(descriptor)


def write_text(name: str, text: str, *, append: bool = False) -> None:
    flags = os.O_WRONLY | os.O_CREAT
    if append:
        flags |= os.O_APPEND
    descriptor = open_file(name, flags)
    try:
        if not append:
            os.ftruncate(descriptor, 0)
        remaining = memoryview(text.encode("utf-8"))
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("short write to Dunsel state file")
            remaining = remaining[written:]
    finally:
        os.close(descriptor)


def read_text(name: str, *, max_bytes: int = 4096) -> str:
    descriptor = open_file(name, os.O_RDONLY | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if info.st_size > max_bytes:
            raise ValueError("Dunsel state file exceeds its read bound")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 4096))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > max_bytes:
            raise ValueError("Dunsel state file exceeds its read bound")
        return raw.decode("utf-8")
    finally:
        os.close(descriptor)


def read_tail(name: str, max_bytes: int) -> tuple[os.stat_result, bytes]:
    descriptor = open_file(name, os.O_RDONLY | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        os.lseek(descriptor, max(0, info.st_size - max_bytes), os.SEEK_SET)
        chunks: list[bytes] = []
        remaining = max_bytes
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return info, b"".join(chunks)
    finally:
        os.close(descriptor)


def write_process_identity(pid: int) -> None:
    """Pin PID, kernel start time and exact arguments across local checkouts."""
    start, args = _proc_identity(pid)
    write_text(PID_PENDING_FILE, json.dumps({"pid": pid, "start": start, "args": args}) + "\n")
    descriptor = open_file(PID_PENDING_FILE, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory = _open_state_dir()
    try:
        os.replace(PID_PENDING_FILE, PID_FILE, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        os.close(directory)


def process_identity() -> dict | None:
    """Return only a still-matching worker; PID reuse cannot target another process."""
    try:
        record = json.loads(read_text(PID_FILE))
        pid, args = record["pid"], record["args"]
        if (type(pid) is not int or pid <= 0 or not isinstance(args, list)
                or len(args) != 4 or not all(isinstance(arg, str) for arg in args)
                or args[2:] != ["--instance", "dunsel"]
                or not Path(args[0]).is_absolute() or not Path(args[1]).is_absolute()
                or Path(args[1]).name != "marshall_dunsel.py"):
            raise ValueError("invalid Dunsel process identity")
        start, current = _proc_identity(pid)
        if current != args or start != record["start"]:
            return None
        return {"pid": pid, "command": " ".join(args), "args": args}
    except FileNotFoundError:
        return None
    except (KeyError, TypeError, IndexError):
        raise ValueError("invalid Dunsel process identity") from None


def _proc_identity(pid: int) -> tuple[str, list[str]]:
    root = Path("/proc") / str(pid)
    raw = (root / "stat").read_text(encoding="ascii")
    start = raw[raw.rfind(")") + 2:].split()[19]
    args = [value.decode("utf-8") for value in (root / "cmdline").read_bytes().split(b"\0") if value]
    return start, args


@contextmanager
def locked_control():
    """Serialize every state mutation by controllers and the worker."""
    descriptor = open_file(LOCK_FILE, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def record_startup(pid: int) -> None:
    """Retain launched process exposure before its worker arguments are visible."""
    start, _ = _proc_identity(pid)
    write_text(STARTUP_FILE, json.dumps({"pid": pid, "start": start}) + "\n")


def startup_alive() -> bool:
    """Refuse retries while a failed start may still be alive; unknowns fail closed."""
    try:
        record = json.loads(read_text(STARTUP_FILE))
    except FileNotFoundError:
        return file_exists(INTENT_FILE)
    if not isinstance(record, dict) or type(record.get("pid")) is not int or record["pid"] <= 0:
        raise ValueError("invalid Dunsel startup record")
    try:
        start, _ = _proc_identity(record["pid"])
    except FileNotFoundError:
        return False
    return start == record["start"]


def record_launch_intent() -> None:
    """Persist blocking intent before Popen; failed publication cannot permit retry."""
    write_text(INTENT_FILE, "launch pending; physical process exposure is unknown\n")
    descriptor = open_file(INTENT_FILE, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory = _open_state_dir()
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
