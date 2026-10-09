"""Agent worktrees under .claude/worktrees: which are clean, which hold someone's work.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .. import hub as sv

if TYPE_CHECKING:      # names used in annotations only (strings at runtime)
    from ..settings import Config


# ---------------------------------------------------------------- worktrees

@dataclass
class WorktreeRow:
    path: Path
    branch: str
    latest: float          # later of last commit and creation
    latest_kind: str       # "commit" or "created"
    dirty: int | None
    agent_state: str | None = None


def read_worktrees(cfg: Config, show_dirty: bool) -> list[WorktreeRow]:
    porcelain = sv._run(["git", "worktree", "list", "--porcelain"], cwd=cfg.repo)
    rows: list[WorktreeRow] = []
    current: dict[str, str] = {}
    for line in porcelain.splitlines() + [""]:
        if not line.strip():
            if current.get("worktree"):
                rows.append(_worktree_row(cfg, current, show_dirty))
            current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    return [r for r in rows if r.path != cfg.repo]


def _worktree_row(cfg: Config, entry: dict[str, str], show_dirty: bool) -> WorktreeRow:
    path = Path(entry["worktree"])
    branch = entry.get("branch", "").replace("refs/heads/", "") or (
        "detached" if "detached" in entry else "?")

    commit_ts = 0.0
    out = sv._run(["git", "log", "-1", "--format=%ct"], cwd=path, timeout=5.0)
    try:
        commit_ts = float(out.strip())
    except ValueError:
        pass

    # A worktree's creation time is the birth of its admin directory under
    # .git/worktrees/<name> — the checkout itself gets touched by every build.
    admin = cfg.git_dir / "worktrees" / path.name
    created = sv.birth_time(admin)
    if created is None:
        try:
            created = admin.stat().st_mtime
        except OSError:
            created = 0.0

    latest, kind = (commit_ts, "commit") if commit_ts >= created else (created, "created")

    dirty = None
    if show_dirty:
        status = sv._run(["git", "--no-optional-locks", "status", "--porcelain"], cwd=path, timeout=5.0)
        dirty = len([ln for ln in status.splitlines() if ln.strip()])

    return WorktreeRow(path, branch, latest, kind, dirty)
