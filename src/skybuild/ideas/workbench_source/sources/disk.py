"""Disk: the pool's usage, sampled, and the Disk section.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .. import hub as sv
from ..sources.lease import run_cmd

if TYPE_CHECKING:      # names used in annotations only (strings at runtime)
    from ..sections.work import Runner


def human_bytes(n: float | None) -> str:
    if n is None:
        return "?"
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


@dataclass(frozen=True)
class DiskUsage:
    source: str
    used: int
    free: int

    @property
    def capacity(self) -> int:
        return self.used + self.free

    @property
    def pct(self) -> float:
        return 100.0 * self.used / self.capacity if self.capacity else 0.0


def pool_of(path: Path, runner: Runner = run_cmd) -> str | None:
    """The ZFS pool holding `path`, from the mount's source; None when it is not ZFS."""
    rc, out = runner(["findmnt", "-n", "-o", "FSTYPE,SOURCE", "--target", str(path)])
    parts = out.split()
    if rc != 0 or len(parts) < 2 or parts[0] != "zfs":
        return None
    return parts[1].split("/")[0] or None


def read_disk(path: Path, zpool: str | None, runner: Runner = run_cmd) -> DiskUsage | None:
    """The pool's allocation when the repo sits on ZFS, else the filesystem's.

    A ZFS dataset's free space is the pool's, shared with every other dataset,
    so the pool is what fills; `zpool list` says how full it is.
    """
    pool = zpool or pool_of(path, runner)
    if pool:
        rc, out = runner(["zpool", "list", "-Hp", "-o", "name,allocated,free", pool])
        fields = out.split()
        if rc == 0 and len(fields) >= 3 and fields[0] == pool:
            try:
                used, free = int(fields[1]), int(fields[2])
            except ValueError:
                used = free = -1
            if used >= 0 and free >= 0 and used + free > 0:
                return DiskUsage(f"zpool {pool}", used, free)
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    used = (st.f_blocks - st.f_bfree) * st.f_frsize
    free = st.f_bavail * st.f_frsize
    if used + free <= 0:
        return None
    return DiskUsage(f"filesystem at {path}", used, free)


class DiskSampler:
    """Recent disk samples and a straight-line fit of how fast the disk fills.

    Kept in memory only: this server writes nothing but the cleanup, so the
    prediction starts empty on every start and says so until it has enough.
    """

    def __init__(self, window_seconds: float, min_span_seconds: float, spacing_seconds: float) -> None:
        self.window = window_seconds
        self.min_span = min_span_seconds
        self.spacing = spacing_seconds
        self.samples: list[tuple[float, int]] = []
        self.lock = threading.Lock()

    def add(self, now: float, usage: DiskUsage | None) -> None:
        if usage is None:
            return
        with self.lock:
            if self.samples and now - self.samples[-1][0] < self.spacing:
                return
            self.samples.append((now, usage.used))
            self.samples = [s for s in self.samples if now - s[0] <= self.window]

    def predict(self, now: float, usage: DiskUsage | None, line_pct: float) -> dict:
        with self.lock:
            points = [s for s in self.samples if now - s[0] <= self.window]
        span = points[-1][0] - points[0][0] if len(points) >= 2 else 0.0
        out: dict = {"samples": len(points), "span": sv.age(span), "state": "gathering",
                     "needs": sv.age(self.min_span)}
        if usage is None:
            out["state"] = "unknown"
            return out
        if len(points) < 2 or span < self.min_span:
            return out
        mean_t = sum(t for t, _ in points) / len(points)
        mean_u = sum(u for _, u in points) / len(points)
        var = sum((t - mean_t) ** 2 for t, _ in points)
        slope = sum((t - mean_t) * (u - mean_u) for t, u in points) / var if var else 0.0
        out["rate_per_hour"] = slope * 3600
        out["rate_text"] = f"{'+' if slope >= 0 else '-'}{human_bytes(abs(slope) * 3600)}/h"
        if slope <= 0:
            out["state"] = "steady"
            return out
        line_bytes = usage.capacity * line_pct / 100
        out["state"] = "growing"
        out["full_in_seconds"] = max(0.0, usage.free / slope)
        out["line_in_seconds"] = max(0.0, (line_bytes - usage.used) / slope)
        out["full_in"] = sv.age(out["full_in_seconds"])
        out["line_in"] = sv.age(out["line_in_seconds"])
        return out


def disk_section(usage: DiskUsage | None, prediction: dict, line_pct: float) -> dict:
    if usage is None:
        return {"error": "the disk could not be read", "prediction": prediction, "line_pct": line_pct}
    return {
        "error": "",
        "source": usage.source,
        "used": human_bytes(usage.used),
        "free": human_bytes(usage.free),
        "capacity": human_bytes(usage.capacity),
        "pct": round(usage.pct, 1),
        "line_pct": line_pct,
        "over_line": usage.pct >= line_pct,
        "prediction": prediction,
    }
