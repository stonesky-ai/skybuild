"""Work in flight: the lane processes (gate_run, gate_lane, pytest, testfast) and who owns each.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .. import hub as sv

if TYPE_CHECKING:      # names used in annotations only (strings at runtime)
    from ..sources.sessions import Session


# ---------------------------------------------------------------- work in flight

# The main session runs its own lanes through Bash — pytest, compose, migrations.
# None of that is a sub-agent, so a hard-working session shows "0 sub-agents" and
# reads as idle unless the lanes themselves are surfaced. That is what this is for.
LANE_KINDS = (
    ("unit lane", re.compile(r"(^|/)pytest\b|-m\s+pytest\b")),
    ("compose", re.compile(r"docker[-\s]+compose\b")),
    ("migration", re.compile(r"\bmigrate(\.py)?\b|\balembic\b")),
)
_FLAGLESS = re.compile(r"^[^-]")


@dataclass
class LaneRow:
    kind: str
    detail: str
    started: float
    session_pid: int | None
    cwd: str | None = None      # names the checkout when no session owns the lane


def _parent_map(pids) -> dict[int, int]:
    """pid -> parent pid, from /proc/<pid>/stat; a process gone meanwhile is left out."""
    parents: dict[int, int] = {}
    for pid in pids:
        fields = sv._stat_fields(pid)
        if fields:
            try:
                parents[pid] = int(fields[1])
            except (IndexError, ValueError):
                pass
    return parents


def read_lanes(sessions: list[Session]) -> list[LaneRow]:
    procs = {pid: cmd for pid, cmd in sv.iter_processes()}
    parents = _parent_map(procs)
    session_pids = {s.pid: s for s in sessions}

    rows: list[LaneRow] = []
    for pid, cmd in procs.items():
        # `timeout 1800 … pytest …` wraps the real lane: count the lane once, on
        # the process actually running it, not on its wrapper or its shell.
        if " -c " in cmd[:200] and "pytest" not in cmd.split(" -c ")[0]:
            continue
        for kind, pattern in LANE_KINDS:
            if not pattern.search(cmd):
                continue
            if kind == "unit lane" and "-m pytest" not in cmd and "/pytest" not in cmd:
                continue
            rows.append(LaneRow(kind, _lane_detail(kind, cmd), sv._proc_start(pid),
                                _owning_session(pid, parents, session_pids), _proc_cwd(pid)))
            break
    # One lane can appear as a wrapper plus its child; keep the oldest per detail.
    best: dict[tuple[str, str], LaneRow] = {}
    for row in rows:
        key = (row.kind, row.detail)
        if key not in best or row.started < best[key].started:
            best[key] = row
    return sorted(best.values(), key=lambda r: r.started)


def _lane_detail(kind: str, cmd: str) -> str:
    parts = cmd.split()
    if kind == "unit lane":
        # `-p no:randomly` and friends take a value; that value is not a target.
        tail = parts[parts.index("pytest") + 1:] if "pytest" in parts else []
        targets, skip = [], False
        for token in tail:
            if skip:
                skip = False
                continue
            if token.startswith("-"):
                skip = token in ("-p", "-k", "-m", "-n", "-o", "--deselect", "--ignore")
                continue
            if token.endswith("pytest"):
                continue
            targets.append(token)
        return "pytest " + (" ".join(targets[:3]) or "(default paths)")
    if kind == "compose":
        project = ""
        for flag in ("-p", "--project-name"):
            if flag in parts:
                try:
                    project = parts[parts.index(flag) + 1]
                except IndexError:
                    pass
        verbs = [p for p in parts if p in ("up", "down", "build", "restart", "ps", "logs", "exec")]
        return f"compose {verbs[0] if verbs else '?'} {project}".strip()
    return "migrate " + next((p for p in parts if "migrate" in p or "alembic" in p), "")


def _proc_cwd(pid: int) -> str | None:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None


def _owning_session(pid: int, parents: dict[int, int],
                    session_pids: dict[int, Session]) -> int | None:
    """Walk up the parent chain to the session that launched this work."""
    seen = 0
    cur = pid
    while cur > 1 and seen < 40:
        if cur in session_pids:
            return cur
        cur = parents.get(cur, 0)
        seen += 1
    return None
