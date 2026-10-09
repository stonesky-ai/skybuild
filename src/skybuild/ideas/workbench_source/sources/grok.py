"""The Grok loop's workers: their logs, models, summaries and liveness.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote

from .. import hub as sv

if TYPE_CHECKING:      # names used in annotations only (strings at runtime)
    from ..frame import GrokWorker


# Grok's shared log only grows; the panel reads this much of its end. A session
# none of whose lines are in it is not live.
GROK_LOG_TAIL_BYTES = 4 * 1024 * 1024
_GROK_SUMMARY_MAX_BYTES = 262144
# A grok process's end lines. `session_end.worker_join` is the one the CLI
# writes; it carries the pid but no sid.
_GROK_END = ("session_end", "session.end", "shell.exit")


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def read_grok_log(path: Path) -> list[dict]:
    """The last GROK_LOG_TAIL_BYTES of Grok's shared log, one dict per whole JSON line."""
    try:
        with path.open("rb") as fh:
            start = max(0, fh.seek(0, os.SEEK_END) - GROK_LOG_TAIL_BYTES)
            fh.seek(start)
            raw = fh.read()
    except OSError:
        return []
    if start:
        raw = raw.partition(b"\n")[2]          # the seek cut the first line
    entries: list[dict] = []
    for line in raw.splitlines():
        try:
            entry = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def _grok_ts(value: object) -> float:
    """A log line's `ts` as epoch seconds; anything unreadable is 0."""
    if not isinstance(value, str):
        return 0.0
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return 0.0


def _proc_argv(pid: int) -> list[str]:
    """A process's own argv. An unreadable one is empty: no argv, no worker."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", "replace") for part in raw.rstrip(b"\0").split(b"\0")] if raw else []


def _grok_model(argv: list[str]) -> str | None:
    """The model a grok process was started with: `-m X`, `--model X` or `--model=X`."""
    for i, arg in enumerate(argv[1:], start=1):
        if arg in ("-m", "--model"):
            return argv[i + 1] if i + 1 < len(argv) else None
        if arg.startswith("--model="):
            return arg.partition("=")[2]
    return None


def _grok_summary(session: Path) -> dict:
    """The session's `summary.json`, for its title. Missing or unreadable is empty."""
    path = session / "summary.json"
    try:
        if path.stat().st_size > _GROK_SUMMARY_MAX_BYTES:
            return {}
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


def _newest_session(folder: Path) -> Path | None:
    """The most recently touched session-id directory of one cwd."""
    try:
        children = list(folder.iterdir())
    except OSError:
        return None
    best: tuple[tuple[float, str], Path] | None = None
    for child in children:
        try:
            if not child.is_dir():
                continue
            key = (child.stat().st_mtime, child.name)
        except OSError:
            continue
        if best is None or key > best[0]:
            best = (key, child)
    return best[1] if best else None


def _grok_worker(folder: Path, session: Path, log: list[dict]) -> GrokWorker | None:
    """The session's worker, or None: a session directory outlives its process.

    The pid is the one on the session's last log line. The session is over once
    that pid logs an end (which carries no sid) or a line for another session,
    and a pid that is gone, or now runs something other than grok, is nobody.
    The panel's empty state is "no live Grok worker".
    """
    sid = session.name
    last = next((i for i in range(len(log) - 1, -1, -1) if log[i].get("sid") == sid), None)
    if last is None:
        return None
    pid = log[last].get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    seen = 0.0
    for entry in log[last:]:
        if entry.get("pid") != pid:
            continue
        other = entry.get("sid")
        if other and other != sid:
            return None
        if str(entry.get("msg") or "").startswith(_GROK_END):
            return None
        seen = max(seen, _grok_ts(entry.get("ts")))
    if not sv._pid_alive(pid):
        return None
    argv = sv._proc_argv(pid)
    if not argv or not Path(argv[0]).name.startswith("grok"):
        return None
    summary = _grok_summary(session)
    model = _grok_model(argv) or summary.get("current_model_id") or "?"
    title = summary.get("session_summary") or summary.get("generated_title") or "(no description)"
    return sv.GrokWorker(sv._shown(str(model)), sv._shown(str(title)), sv._shown(unquote(folder.name)),
                      sv._shown(sid), pid, seen)


def read_grok_workers(home: Path | None) -> list[GrokWorker]:
    """Live workers from Grok's own files under its home. Missing files are no worker, not an error.

    `sessions/<url-encoded cwd>/` holds one directory per session; the newest of
    each cwd is read against the tail of `logs/unified.jsonl`.
    """
    if home is None:
        return []
    try:
        folders = sorted(path for path in (home / "sessions").iterdir() if path.is_dir())
    except OSError:
        return []
    sessions = [(folder, newest) for folder in folders
                if (newest := _newest_session(folder)) is not None]
    if not sessions:
        return []
    log = read_grok_log(home / "logs" / "unified.jsonl")
    return [worker for folder, session in sessions
            if (worker := _grok_worker(folder, session, log)) is not None]


def _grok_worker_line(worker: GrokWorker, now: float | None = None,
                      stale_minutes: float | None = None) -> str:
    """One worker: live or quiet (alive but silent past the bound), as the Claude sub-agents are."""
    silent = now - worker.last_seen if now is not None and worker.last_seen > 0 else None
    quiet = silent is not None and stale_minutes is not None and silent > stale_minutes * 60
    seen = f" · last log {sv.age(silent)} ago" if silent is not None else ""
    return (f"   {'quiet' if quiet else 'live'} {worker.model}  {worker.session_id or 'session ?'}"
            f"  pid {worker.pid}{seen}  {worker.description}  [{worker.worktree}]")


def format_grok_panel(workers: list[GrokWorker], now: float | None = None,
                      stale_minutes: float | None = None) -> str:
    if not workers:
        return "no live Grok worker"
    return "\n".join([f"{len(workers)} live",
                      *(_grok_worker_line(worker, now, stale_minutes) for worker in workers)])
