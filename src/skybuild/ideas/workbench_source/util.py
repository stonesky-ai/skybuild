"""Small helpers shared by every collector: running a command, ages and stamps, a process's birth,
and the watchdog unit's state.
"""
from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from . import hub as sv

if TYPE_CHECKING:      # names used in annotations only (strings at runtime)
    from .settings import Config


# ---------------------------------------------------------------- small helpers

def _run(cmd: list[str], cwd: Path | None = None, timeout: float = 10.0) -> str:
    try:
        r = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True,
                           text=True, timeout=timeout, check=False)
        return r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def iso_epoch(text: str) -> float | None:
    """An ISO-8601 time as an epoch, or None: the todo service writes `...T...Z`, UTC, with or without fraction."""
    raw = text.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.timestamp()


def age(seconds: float) -> str:
    """Compact age: 42s, 7m, 3h12m, 2d4h; "unknown" past a year (a start stamp of 0 measured from now)."""
    s = int(max(0, seconds))
    if s > 365 * 86400:
        return "unknown"
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h{m:02d}m"
    d, h = divmod(h, 24)
    return f"{d}d{h:02d}h"


def local(epoch: float) -> datetime:
    """Epoch seconds as a timezone-aware local datetime."""
    return datetime.fromtimestamp(epoch, tz=UTC).astimezone()


def stamp(epoch: float) -> str:
    return local(epoch).strftime("%Y-%m-%d %H:%M")


def birth_time(path: Path) -> float | None:
    """Creation time where the filesystem records one (ZFS/ext4 do), else None."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    for attr in ("st_birthtime", "st_btime"):
        v = getattr(st, attr, None)
        if v:
            return float(v)
    out = sv._run(["stat", "--printf=%W", str(path)], timeout=5.0)
    try:
        v = int(out.strip())
        return float(v) if v > 0 else None
    except ValueError:
        return None


# ---------------------------------------------------------------- watchdog

@dataclass
class Watchdog:
    pids: list[int] = field(default_factory=list)
    log_age: float | None = None
    log_path: Path | None = None

    @property
    def running(self) -> bool:
        return bool(self.pids)


def read_watchdog(cfg: Config) -> Watchdog:
    pids = [pid for pid, cmd in sv.iter_processes() if cfg.watchdog_pattern in cmd]
    log_age = None
    if cfg.watchdog_log and cfg.watchdog_log.exists():
        log_age = time.time() - cfg.watchdog_log.stat().st_mtime
    return Watchdog(pids, log_age, cfg.watchdog_log)
