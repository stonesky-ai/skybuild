"""The Work, Seams, Integrator, In-process and Next-up sections, and the Unresolved count.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from .. import hub as sv

# ---------------------------------------------------------------- collectors

_QUESTION = re.compile(r"^(\d+)\.\s+(.*\S)\s*$")


def read_unresolved(path: Path) -> dict:
    """`N. question (default)` lines of Unresolved.md. Unreadable is said, never 0."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"error": f"cannot read {path.name}: {exc.strerror or exc}", "count": None, "items": []}
    items = [{"number": int(m.group(1)), "text": sv._shown(m.group(2))}
             for line in text.splitlines() if (m := _QUESTION.match(line))]
    return {"error": "", "count": len(items), "items": items}


#: Which agent family a holder string names, checked in this order. The
#: holder is the seam tip's `Agent: <stream>/<vendor>-<model>` trailer.
VENDORS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("claude", ("claude", "opus", "sonnet", "haiku", "fable")),
    ("codex", ("codex", "gpt")),
    ("grok", ("grok",)),
)


def vendor_of(agent: str) -> str:
    """claude, codex or grok from an `Agent:` value; `other` or `unknown` otherwise."""
    if not agent.strip():
        return "unknown"
    who = agent.lower().partition("/")[2] or agent.lower()
    for vendor, hints in VENDORS:
        if any(hint in who for hint in hints):
            return vendor
    return "other"


def _seam_facts(seam, now: float) -> dict:
    return {
        "holder": sv._shown(seam.agent),
        "vendor": vendor_of(seam.agent),
        "stage": seam.stage,
        "stage_name": seam.stage_name,
        "tip_age": sv.age(now - seam.tip) if seam.tip > 0 else "",
        "next": sv._shown(seam.next_actor),
        "stream": sv._shown(seam.stream),
        "fixup": bool(seam.fixup and seam.stage <= 3),
        "reason": sv._shown(seam.reason),
    }


def work_split(items: list, seams: list, now: float) -> dict:
    """MasterToDo rows: in process, halted, waiting to be integrated, yet to start.

    A pushed `seam/<Id>` is the claim (docs/dev/streams/README.md), so a row
    with none is yet to start whatever anybody intends. A claimed row whose
    seam is STUCK (parked, blocked, or idle past the bound) is halted; one at
    stage 4 or later waits on the integrator; the rest are in process. The
    claim list is the refs of the last fetch: this page never fetches.
    """
    by_id = {s.row_id: s for s in seams}
    split: dict[str, list[dict]] = {"in_process": [], "halted": [], "waiting": [],
                                    "yet_to_start": []}
    holders: dict[str, int] = {}
    for item in sv._by_priority(items):
        row = {"id": sv._shown(item.row_id), "priority": item.priority,
               "text": sv._shown(item.text)}
        seam = by_id.get(item.row_id)
        if seam is None:
            split["yet_to_start"].append(row)
            continue
        row.update(_seam_facts(seam, now))
        if seam.stuck:
            row["why"] = sv._shown(seam.stuck)
            split["halted"].append(row)
        elif seam.stage >= 4:
            split["waiting"].append(row)
        else:
            split["in_process"].append(row)
        holders[row["vendor"]] = holders.get(row["vendor"], 0) + 1
    return {**split, "total": len(items), "holders": holders,
            "counts": {key: len(rows) for key, rows in split.items()}}


def seams_section(seams: list, fetched: float | None, now: float, todo_ids: set[str]) -> dict:
    """The pushed seams: count per board column, and the ones waiting to be landed."""
    columns = {col: sum(1 for s in seams if not s.stuck and s.column == col)
               for col in sv.BOARD_COLUMNS}
    ready = sorted((s for s in seams if s.stage == 4 and not s.stuck), key=lambda s: s.tip)
    waiting = [{"id": sv._shown(s.row_id), **_seam_facts(s, now), "wait_seconds": max(0.0, now - s.tip)}
               for s in ready]
    return {
        "pushed": len(seams),
        "columns": columns,
        "stuck": sum(1 for s in seams if s.stuck),
        "ready": waiting,
        "oldest_ready_seconds": waiting[0]["wait_seconds"] if waiting else None,
        "off_the_list": sorted(sv._shown(s.row_id) for s in seams if s.row_id not in todo_ids),
        "fetched_age": sv.age(now - fetched) if fetched else None,
    }


def integrator_section(seams: list, now: float) -> list[dict]:
    """Every seam pushed and not yet merged to trunk: the integrator's own queue.

    The work is done — pushed — but has not reached trunk, so it is the
    integrator's to process next. A merged seam (its ledger commit pending,
    or already landed) has left this queue, whatever stage or trailers it
    carries; oldest tip first, since that row has waited longest. `reason`
    is why the seam waits: `stuck` when that is set (a block, a park, a
    hold), otherwise the structural `reason`. An empty wait stays off the
    row, as before.
    """
    waiting = sorted((s for s in seams if not s.merged), key=lambda s: s.tip)
    rows = []
    for seam in waiting:
        row = {"id": sv._shown(seam.row_id), "stream": sv._shown(seam.stream) or "?",
               "age": sv.age(now - seam.tip) if seam.tip > 0 else "?"}
        why = sv._shown(seam.stuck or seam.reason)
        if why:
            row["reason"] = why
        rows.append(row)
    return rows


def in_process_section(cfg) -> list[dict]:
    """Every claimed, unlanded row and who holds it — `claims.py`'s own signal.

    Sourced through that module's own `claims()` (never a forked copy of its
    claim-parsing logic): a `seam/<Id>` branch still on the remote is claimed
    and unlanded; landing a seam deletes its branch (AGENTS.md § Shared
    checkout), so a row leaves this box the moment `claims.py` itself no
    longer lists it. A refusal — no remote or trunk configured, an
    unreachable remote — is an empty box, the same shape as no claims: this
    must never raise past the page.
    """
    try:
        remote = sv.claims.claim.remote_name(cfg.remote)
        trunk = sv.claims.trunk_name(cfg.trunk)
        _base, rows = sv.claims.claims(cfg.repo, remote, trunk)
    except sv.claims.Refused:
        return []
    return [{"id": branch.removeprefix("seam/"), "agent": holder}
            for branch, holder, _pushed, _ahead in rows
            if branch.startswith("seam/") and branch.removeprefix("seam/")]


#: A `| Id | ... | Do |` header row, however the other columns are named or
#: spaced — the same shape `sv.read_todo_items` itself looks for, checked
#: here only to tell "no such table" apart from "table present, zero rows".
_TODO_HEADER = re.compile(r"^\s*\|\s*id\s*\|.*\|\s*do\s*\|?\s*$", re.IGNORECASE | re.MULTILINE)


def next_up_section(cfg, claimed_ids: set[str]) -> dict:
    """Open MasterToDo rows nobody has claimed yet, priority order (1 first).

    Rows come from `sv.read_todo_items` — the same reader `work_split` already
    trusts for this table, never a second parser for it. `claimed_ids` is
    `in_process_section`'s own list turned into a set by the caller (the
    Board builds both from one cached read), so a row leaves this box the
    instant it leaves that one: one source of truth, not a forked copy of its
    claim-parsing logic. A file that cannot be read, or one with no
    recognisable `| Id | ... | Do |` table, is an empty box with a warning
    line — never a raised exception past the page.
    """
    try:
        text = cfg.todo.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"error": f"cannot read {cfg.todo.name}: {exc.strerror or exc}", "count": None, "rows": []}
    if not _TODO_HEADER.search(text):
        return {"error": f"{cfg.todo.name} has no `| Id | ... | Do |` table to parse",
                "count": None, "rows": []}
    items = [item for item in sv._by_priority(sv.read_todo_items(cfg.todo))
             if item.row_id not in claimed_ids]
    return {"error": "", "count": len(items),
            "rows": [{"id": sv._shown(item.row_id), "priority": item.priority,
                     "text": sv._shown(item.text)} for item in items]}


def sessionview_panels(cfg, frame, workers: list) -> dict:
    """sessionview's own panels and terminal frame, escaped and badged by sessionview."""
    plain = replace(cfg, color=False)
    once = sv.render(plain, frame.wd, frame.sessions, frame.agents, frame.lanes, frame.ollama,
                     frame.gpu, frame.worktrees, frame.todo, frame.now, frame.codex, frame.seams,
                     frame.fetched, workers)
    texts = {
        "claude": sv.format_claude_panel(frame.agents),
        "codex": sv.format_codex_panel(frame.codex, frame.now),
        "grok": sv.format_grok_panel(workers, frame.now, cfg.stale_minutes),
        "seams": sv.format_seams_panel(cfg, frame.seams, frame.fetched, frame.now),
        "progress": sv.format_progress_panel(frame.seams, sv.stream_logs(cfg)),
        "once": once,
    }
    return {key: sv.panel_html(key, text) for key, text in texts.items()}


# ---------------------------------------------------------------- disk

Runner = Callable[..., tuple[int | None, str]]
