"""The integrator lease banner: who holds the seat, how fresh the lease is.
"""
from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from .. import hub as sv

if TYPE_CHECKING:      # names used in annotations only (strings at runtime)
    from ..settings import WebConfig


def run_cmd(cmd: list[str], cwd: Path | None = None, timeout: float = 10.0) -> tuple[int | None, str]:
    """(exit code, stdout); None when the command could not run at all. Never raises."""
    try:
        done = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
                              timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None, ""
    return done.returncode, done.stdout


def integrator_lease_banner(wcfg: WebConfig, now: float,
                            reader: Callable[[str, Path], dict | None] | None = None) -> dict:
    """Current lease authority, or an explicit no-integrator answer on any read failure.

    The lease module owns the ref name, document reader and TTL. Its document
    currently has no pid; show one only when a future writer supplies it.
    """
    try:
        lease = sv._load("sessionview_web_lease", "agents/skybus/lease.py")
        read = reader or (lambda remote, root: lease.holder(remote, root=root))
        doc = read(wcfg.sv.remote, wcfg.sv.repo)
    except Exception:  # noqa: BLE001 - an unreadable authority never means a live holder
        return {"state": "unreadable", "headline": "NO INTEGRATOR RUNNING",
                "detail": "lease unreadable; holder unknown"}
    if doc is None:
        return {"state": "absent", "headline": "NO INTEGRATOR RUNNING",
                "detail": "no integrator lease"}
    if not isinstance(doc, dict):
        return {"state": "unreadable", "headline": "NO INTEGRATOR RUNNING",
                "detail": "lease unreadable; holder unknown"}
    host, session, epoch = doc.get("host"), doc.get("session"), doc.get("epoch")
    age = lease._age(doc.get("renewed_at"), now)
    if (not isinstance(host, str) or not host.strip() or
            not isinstance(session, str) or not session.strip() or
            not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1 or age is None):
        return {"state": "unreadable", "headline": "NO INTEGRATOR RUNNING",
                "detail": "lease unreadable; holder unknown"}
    pid = doc.get("pid")
    pid_text = str(pid) if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 else "unknown"
    detail = (f"session {sv._shown(session)} · host {sv._shown(host)} · epoch {epoch}"
              f" · pid {pid_text} · renewed {sv.age(max(0.0, age))} ago")
    if age > lease.LEASE_TTL_SECONDS:
        return {"state": "stale", "headline": "STALE LEASE — NO INTEGRATOR RUNNING",
                "detail": detail}
    return {"state": "fresh", "headline": "INTEGRATOR RUNNING", "detail": detail}
