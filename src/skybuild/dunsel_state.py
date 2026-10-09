"""Private, no-follow state files for the local Dunsel preview utility."""
from __future__ import annotations

import os
import stat
from pathlib import Path


STATE_DIR = Path.home() / ".local" / "state" / "skybuild" / "dunsel"
LOG_FILE = "dunsel.log"
EXIT_FILE = "dunsel.off-now"
DISABLED_FILE = "dunsel.disabled"
PID_FILE = "dunsel.pid"
LOCK_FILE = "dunsel.control.lock"
_STATE_FILES = {LOG_FILE, EXIT_FILE, DISABLED_FILE, PID_FILE, LOCK_FILE}
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
