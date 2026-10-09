"""Serialize temporary worktree creation against the shared checkout limit."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import subprocess


MAX_WORKTREES = 64


class WorktreeCapacityError(RuntimeError):
    """The repository lacks the requested reserved worktree capacity."""


def _git(checkout: Path, *arguments: str) -> str:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    result = subprocess.run(["git", *arguments], cwd=checkout, env=env, text=True,
                            capture_output=True, check=False, timeout=15)
    if result.returncode:
        raise WorktreeCapacityError("Could not verify repository worktree capacity")
    return result.stdout.strip()


def _worktree_count(checkout: Path) -> int:
    listed = _git(checkout, "worktree", "list", "--porcelain")
    return sum(line.startswith("worktree ") for line in listed.splitlines())


@contextmanager
def reserve_worktree_slots(checkout: Path, slots: int):
    """Hold the capacity lock through count verification and worktree creation.

    Once registered, worktrees count against later reservations without retaining
    this lock. Callers may hold it longer when their operation requires that.
    """
    if type(slots) is not int or not 1 <= slots <= MAX_WORKTREES:
        raise WorktreeCapacityError("Requested worktree reserve is invalid")
    common_dir = Path(_git(checkout, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    lock_path = common_dir / "skybuild-worktree-capacity.lock"
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        count = _worktree_count(checkout)
        if count + slots > MAX_WORKTREES:
            raise WorktreeCapacityError(
                f"Need {slots} free worktree slot(s); current count is {count} of {MAX_WORKTREES}")
        yield count


@contextmanager
def serialize_integrations(checkout: Path):
    """Serialize cooperating local integrators without blocking preparation.

    Acquire this lock before the capacity lock. Preparers never acquire this
    lock. This does not fence GitHub or other noncooperating publishers.
    """
    common_dir = Path(_git(checkout, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    with (common_dir / "skybuild-integration.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield
