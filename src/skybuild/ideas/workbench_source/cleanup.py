"""Disk cleanup, the page's only destructive action: it plans, names every directory, and deletes only
what was ticked.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import shutil
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from . import hub as sv

if TYPE_CHECKING:      # names used in annotations only (strings at runtime)
    from .sections.work import Runner
    from .settings import WebConfig
    from .sources.loops_memory import LoopUnit


# ---------------------------------------------------------------- cleanup: the only write
#
# Two steps, and the owner is between them. A plan names every directory it
# would delete, with its size and why it is safe, and holds back (with the
# reason) everything it considered and would not touch. A run deletes only
# items of that plan the caller names, only after re-checking each one, and
# all-or-nothing: one item that no longer passes refuses the whole run.
# A plan is used once and expires. Only the kinds in SAFE_KINDS exist; the
# rules are the ones scripts/cleanup_run.sh learned (a worktree with work
# in it is HELD, never removed), and nothing here names a docker volume, a
# container, an image or anything outside this checkout.

CACHE_DIR_NAMES = frozenset({"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"})
#: Never walked into while looking for caches: git's own data, virtual environments.
NO_WALK = frozenset({".git", ".venv", "node_modules"})


@dataclass(frozen=True)
class CleanupItem:
    item_id: str
    kind: str
    path: str                       # the worktree, or the checkout the caches belong to
    size: int | None
    why: str
    targets: tuple[str, ...]        # exactly the directories a run deletes


@dataclass(frozen=True)
class Held:
    kind: str
    path: str
    why: str


@dataclass
class CleanupContext:
    repo: Path
    worktrees_root: Path
    trunk: str | None
    min_age_seconds: float
    now: float
    busy_worktrees: set[str]                    # worktree names a live sub-agent works in
    procs: list[tuple[int, str, str]]           # (pid, cwd, command line) of other processes
    runner: Runner


def _item_id(kind: str, path: str) -> str:
    return hashlib.sha256(f"{kind}\0{path}".encode()).hexdigest()[:16]


def _tree_bytes(path: Path) -> int:
    total = 0
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        for name in dirnames + filenames:
            try:
                st = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            total += st.st_blocks * 512
    return total


def _du(path: Path, runner: Runner) -> int | None:
    rc, out = runner(["du", "-sxB1", str(path)], timeout=120.0)
    try:
        return int(out.split()[0]) if rc == 0 else None
    except (IndexError, ValueError):
        return None


def worktree_entries(repo: Path, runner: Runner) -> list[dict[str, str]]:
    """`git worktree list --porcelain`, one dict per worktree."""
    rc, out = runner(["git", "worktree", "list", "--porcelain"], cwd=repo)
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in (out if rc == 0 else "").splitlines() + [""]:
        if not line.strip():
            if current.get("worktree"):
                entries.append(current)
            current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    return entries


def worktree_verdict(ctx: CleanupContext, entry: dict[str, str]) -> str:
    """Why this worktree is held, or "" when it may be removed. Every check must pass."""
    path = Path(entry.get("worktree", ""))
    root = ctx.worktrees_root.resolve()
    if path.is_symlink() or path.resolve().parent != root or not path.name.startswith("agent-"):
        return "not an agent worktree under .claude/worktrees"
    if "locked" in entry:
        return "locked"
    if "prunable" in entry or not path.is_dir():
        return "its directory is gone (git worktree prune's job, not this one's)"
    if ctx.trunk is None:
        return "no trunk configured, so its commits cannot be proven landed"
    head = entry.get("HEAD", "")
    if not re.fullmatch(r"[0-9a-f]{40,64}", head):
        return "its HEAD could not be read"
    rc, _ = ctx.runner(["git", "merge-base", "--is-ancestor", head, ctx.trunk], cwd=ctx.repo)
    if rc != 0:
        rc_n, count = ctx.runner(["git", "rev-list", "--count", f"{ctx.trunk}..{head}"], cwd=ctx.repo)
        return f"{count.strip() if rc_n == 0 else 'some'} commit(s) not on {ctx.trunk}"
    rc, out = ctx.runner(["git", "-C", str(path), "--no-optional-locks", "status", "--porcelain"])
    if rc != 0:
        return "its working tree could not be read"
    dirty = [line for line in out.splitlines() if line.strip()]
    if dirty:
        return f"{len(dirty)} uncommitted or untracked file(s): somebody's in-flight work"
    if path.name in ctx.busy_worktrees:
        return "a Claude sub-agent is working in it"
    for pid, cwd, cmd in ctx.procs:
        if cwd == str(path) or cwd.startswith(str(path) + "/") or str(path) in cmd:
            return f"pid {pid} runs in it or names it"
    rc, out = ctx.runner(["git", "-C", str(path), "log", "-1", "--format=%ct"])
    try:
        touched = max(path.stat().st_mtime, float(out.strip()) if rc == 0 else 0.0)
    except (OSError, ValueError):
        return "its age could not be read"
    if ctx.now - touched < ctx.min_age_seconds:
        return f"touched {sv.age(ctx.now - touched)} ago, younger than {sv.age(ctx.min_age_seconds)}"
    return ""


def plan_worktrees(ctx: CleanupContext) -> tuple[list[CleanupItem], list[Held]]:
    items, held = [], []
    for entry in worktree_entries(ctx.repo, ctx.runner):
        path = entry.get("worktree", "")
        if not path.startswith(str(ctx.worktrees_root) + "/"):
            continue                                   # the main checkout, scratch worktrees: not ours
        why = worktree_verdict(ctx, entry)
        if why:
            held.append(Held("agent-worktree", path, why))
            continue
        items.append(CleanupItem(_item_id("agent-worktree", path), "agent-worktree", path,
                                 _du(Path(path), ctx.runner),
                                 f"clean, every commit on {ctx.trunk}, nobody in it, idle past "
                                 f"{sv.age(ctx.min_age_seconds)}", (path,)))
    return items, held


def validate_worktree(ctx: CleanupContext, item: CleanupItem) -> str:
    if item.targets != (item.path,):
        return "a worktree item deletes its own directory and nothing else"
    for entry in worktree_entries(ctx.repo, ctx.runner):
        if entry.get("worktree") == item.path:
            return worktree_verdict(ctx, entry)
    return "no longer a registered worktree"


def delete_worktree(ctx: CleanupContext, item: CleanupItem) -> tuple[bool, str]:
    # No --force: git itself refuses a worktree with changes, a second guard.
    rc, _ = ctx.runner(["git", "worktree", "remove", item.path], cwd=ctx.repo, timeout=120.0)
    return rc == 0, "removed" if rc == 0 else "git worktree remove refused"


def cache_dirs(root: Path) -> list[Path]:
    """Every cache directory under `root`, never following a link or entering NO_WALK."""
    found: list[Path] = []
    for dirpath, dirnames, _files in os.walk(root, followlinks=False):
        keep = []
        for name in dirnames:
            full = Path(dirpath) / name
            if full.is_symlink() or name in NO_WALK:
                continue
            if name in CACHE_DIR_NAMES:
                found.append(full)
            else:
                keep.append(name)
        dirnames[:] = keep
    return found


def _checkout_of(ctx: CleanupContext, path: Path) -> str:
    """The checkout a path belongs to: an agent worktree, or the main one."""
    try:
        rel = path.relative_to(ctx.worktrees_root)
    except ValueError:
        return str(ctx.repo)
    return str(ctx.worktrees_root / rel.parts[0])


def plan_caches(ctx: CleanupContext) -> tuple[list[CleanupItem], list[Held]]:
    groups: dict[str, list[Path]] = {}
    for found in cache_dirs(ctx.repo):
        groups.setdefault(_checkout_of(ctx, found), []).append(found)
    items = []
    for checkout, dirs in sorted(groups.items()):
        items.append(CleanupItem(_item_id("python-cache", checkout), "python-cache", checkout,
                                 sum(_tree_bytes(d) for d in dirs),
                                 f"{len(dirs)} regenerable cache dir(s): "
                                 + ", ".join(sorted(CACHE_DIR_NAMES)),
                                 tuple(str(d) for d in sorted(dirs))))
    return items, []


def cache_target_problem(ctx: CleanupContext, target: str) -> str:
    path = Path(target)
    if path.name not in CACHE_DIR_NAMES:
        return f"{target} is not a cache directory"
    if path.is_symlink():
        return f"{target} is a link"
    try:
        lexical = path.relative_to(ctx.repo)
        real = path.resolve().relative_to(ctx.repo.resolve())
    except ValueError:
        return f"{target} is outside {ctx.repo}"
    if lexical != real or ".." in lexical.parts:
        return f"{target} is reached through a link"
    if any(part in NO_WALK for part in lexical.parts):
        return f"{target} is inside {', '.join(sorted(NO_WALK))}, where no cache walk goes"
    return ""


def validate_caches(ctx: CleanupContext, item: CleanupItem) -> str:
    if not item.targets:
        return "names no directory"
    for target in item.targets:
        if _checkout_of(ctx, Path(target)) != item.path:
            return f"{target} is not in {item.path}"
        why = cache_target_problem(ctx, target)
        if why:
            return why
    return ""


def delete_caches(ctx: CleanupContext, item: CleanupItem) -> tuple[bool, str]:
    failed = 0
    for target in item.targets:
        if not os.path.lexists(target):
            continue
        try:
            shutil.rmtree(target)
        except OSError:
            failed += 1
    return failed == 0, "removed" if not failed else f"{failed} director(ies) could not be removed"


@dataclass(frozen=True)
class CleanupKind:
    description: str
    plan: Callable[[CleanupContext], tuple[list[CleanupItem], list[Held]]]
    validate: Callable[[CleanupContext, CleanupItem], str]
    delete: Callable[[CleanupContext, CleanupItem], tuple[bool, str]]


#: The named-safe set. A plan item of any other kind is refused, whatever it names.
SAFE_KINDS: dict[str, CleanupKind] = {
    "agent-worktree": CleanupKind(
        "an agent worktree under .claude/worktrees whose commits are all on trunk, with no "
        "uncommitted or untracked file, no sub-agent or process in it, idle past the minimum age",
        plan_worktrees, validate_worktree, delete_worktree),
    "python-cache": CleanupKind(
        "Python and tool cache directories (" + ", ".join(sorted(CACHE_DIR_NAMES)) + ") inside "
        "this checkout, never inside .git or a virtual environment",
        plan_caches, validate_caches, delete_caches),
}


@dataclass
class CleanupPlan:
    plan_id: str
    created: float
    items: dict[str, CleanupItem]


def _item_json(item: CleanupItem) -> dict:
    return {"id": item.item_id, "kind": item.kind, "path": item.path, "size": item.size,
            "size_text": sv.human_bytes(item.size), "why": item.why, "targets": list(item.targets)}


class Cleanup:
    """Plans and runs. `context` is called for fresh facts at plan time AND at run time."""

    def __init__(self, ttl_seconds: float, context: Callable[[float], CleanupContext]) -> None:
        self.ttl = ttl_seconds
        self.context = context
        self.plans: dict[str, CleanupPlan] = {}
        self.lock = threading.Lock()

    def plan(self, now: float) -> dict:
        ctx = self.context(now)
        items: list[CleanupItem] = []
        held: list[Held] = []
        for kind in SAFE_KINDS.values():
            found, kept = kind.plan(ctx)
            items += found
            held += kept
        plan = CleanupPlan(secrets.token_urlsafe(18), now, {i.item_id: i for i in items})
        with self.lock:
            self.plans = {k: p for k, p in self.plans.items() if now - p.created <= self.ttl}
            self.plans[plan.plan_id] = plan
        total = sum(i.size or 0 for i in items)
        return {"plan": plan.plan_id, "expires_in": sv.age(self.ttl), "items": [_item_json(i) for i in items],
                "held": [{"kind": h.kind, "path": h.path, "why": h.why} for h in held],
                "total": sv.human_bytes(total),
                "kinds": {name: kind.description for name, kind in SAFE_KINDS.items()}}

    def run(self, plan_id: object, item_ids: object, now: float) -> tuple[int, dict]:
        with self.lock:
            plan = self.plans.pop(plan_id, None) if isinstance(plan_id, str) else None
        if plan is None or now - plan.created > self.ttl:
            return 409, {"refused": "no such plan, or it expired or was used: preview again", "deleted": []}
        if (not isinstance(item_ids, list) or not item_ids
                or not all(isinstance(i, str) for i in item_ids)):
            return 400, {"refused": "name at least one item of the plan by its id", "deleted": []}
        unknown = [i for i in item_ids if i not in plan.items]
        if unknown:
            return 400, {"refused": "not in the plan: " + ", ".join(sv._shown(i)[:32] for i in unknown),
                         "deleted": []}
        chosen = [plan.items[i] for i in dict.fromkeys(item_ids)]
        ctx = self.context(now)
        problems = []
        for item in chosen:
            kind = SAFE_KINDS.get(item.kind)
            why = (f"kind {item.kind!r} is outside the named-safe set" if kind is None
                   else kind.validate(ctx, item))
            if why:
                problems.append(f"{item.path}: {why}")
        if problems:
            return 409, {"refused": "nothing was deleted: an item no longer passes its checks",
                         "problems": problems, "deleted": []}
        deleted, failed = [], []
        for item in chosen:
            ok, note = SAFE_KINDS[item.kind].delete(ctx, item)
            (deleted if ok else failed).append({"path": item.path, "kind": item.kind, "note": note})
        return 200, {"deleted": deleted, "failed": failed}


def cleanup_context(wcfg: WebConfig, runner: Runner, units_fn: Callable[[float], list[LoopUnit]],
                    now: float) -> CleanupContext:
    """Fresh facts for a plan or a run: live sub-agents' worktrees, other processes, trunk."""
    cfg = wcfg.sv
    sessions = sv.live_sessions(cfg)
    agents = sv.read_agents(cfg, sessions, sv.TranscriptScanner(), now)
    busy = {a.worktree for a in agents if a.worktree and a.state != "done"}
    mine = {os.getpid()}
    parent = os.getppid()
    while parent > 1 and parent not in mine:
        mine.add(parent)
        fields = sv._stat_fields(parent)
        parent = int(fields[1]) if fields else 0
    procs = [(pid, sv._proc_cwd(pid) or "", cmd) for pid, cmd in sv.iter_processes() if pid not in mine]
    trunk = cfg.trunk
    if trunk is None:
        named = set(sv.unit_flag(units_fn(now), "--trunk"))
        trunk = named.pop() if len(named) == 1 else None
    return CleanupContext(cfg.repo, cfg.repo / ".claude" / "worktrees", trunk,
                          wcfg.cleanup_min_age_hours * 3600, now, busy, procs, runner)


def cleanup_section(wcfg: WebConfig) -> dict:
    """What the Disk cleanup box's buttons may do: the named-safe set and its limits.

    Fixed for the server's life, so the page draws the box once and a refresh
    never wipes a preview the owner is reading.
    """
    return {"may_delete": {name: kind.description for name, kind in SAFE_KINDS.items()},
            "idle_at_least": sv.age(wcfg.cleanup_min_age_hours * 3600),
            "a_plan_expires_after": sv.age(wcfg.plan_ttl_seconds),
            "how": ("Preview names every directory a run would delete and why, and deletes nothing. "
                    "Only the items you tick are run, each re-checked first; if one no longer passes, "
                    "nothing is deleted.")}
