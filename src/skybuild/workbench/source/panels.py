"""The page-only panels: Claude, Codex, seams, the MasterToDo split with claims, the snapshot, and
`panel_html`, which escapes a panel once and adds its badge spans.
"""
from __future__ import annotations

import html
import importlib.util
import re
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from . import hub as sv
from .sources.seams import SEAM_STAGES
from .terminal import _PHASE_TEXT

if TYPE_CHECKING:      # names used in annotations only (strings at runtime)
    from .frame import TodoItem
    from .settings import Config
    from .sources.codex_loop import CodexLoop, CodexReader
    from .sources.ollama import OllamaSampler
    from .sources.seams import SeamRow
    from .sources.sessions import AgentRow, TranscriptScanner
    from .terminal import LedgerDebtRow


_CLAIMS_MOD = None
_CLAIMS_LOCK = threading.Lock()


def format_claude_panel(agents: list[AgentRow]) -> str:
    running = [agent for agent in agents if agent.state == "running"]
    stale = [agent for agent in agents if agent.state == "stale"]
    lines = [f"Sub-agents {len(running)} running" + (f"  +{len(stale)} quiet" if stale else "")]
    for agent in running + stale:
        flag = "quiet" if agent.state == "stale" else "live"
        lines.append(f"   {flag} {agent.model} {agent.description}")
    if not running and not stale:
        lines.append("   (none — no sub-agent is working right now)")
    return "\n".join(lines)


def format_codex_panel(loop: CodexLoop | None, now: float) -> str:
    if loop is None:
        return "no loop on this machine"
    return "\n".join(sv.render_codex(sv.Paint(False), loop, 400, now))


def format_seams_panel(cfg: Config, seams: list[SeamRow], fetched: float | None, now: float,
                       ledger: LedgerDebtRow | None = None) -> str:
    """The seams, then the merges above the last ledger commit; read here when handed none."""
    lines = sv.render_seams(sv.Paint(False), seams, fetched, 400, now, cfg.seam_idle_minutes)
    row = sv.read_ledger_debt(cfg) if ledger is None else ledger
    if row is not None:
        lines.append(sv.render_ledger_debt(sv.Paint(False), row, 400))
    return "\n".join(lines)


def read_todo_items(path: Path) -> list[TodoItem]:
    """`| Id | P | Do |` rows. A missing file is an empty list, not a guess at who holds what."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    items: list[TodoItem] = []
    in_table = False
    p_col = do_col = -1
    for line in lines:
        if not line.lstrip().startswith("|"):
            in_table = False
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if any(re.fullmatch(r"-{2,}|:?-+:?", cell) for cell in cells):
            continue
        lowered = [cell.lower() for cell in cells]
        if lowered[:1] == ["id"] and "do" in lowered:
            in_table = True
            p_col = lowered.index("p") if "p" in lowered else -1
            do_col = lowered.index("do")
            continue
        if not in_table or not cells or not cells[0]:
            continue
        row_id = re.sub(r"[*`]", "", cells[0])
        priority = None
        if 0 <= p_col < len(cells):
            try:
                priority = int(cells[p_col])
            except ValueError:
                priority = None
        text = cells[do_col] if 0 <= do_col < len(cells) else ""
        items.append(sv.TodoItem(row_id, priority, text))
    return items


def _claims_module():
    """`scripts/claims.py`, loaded from beside this file whatever the caller's path."""
    global _CLAIMS_MOD
    if _CLAIMS_MOD is None:
        spec = importlib.util.spec_from_file_location(
            "sessionview_claims", sv.SCRIPTS / "claims.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _CLAIMS_MOD = module
    return _CLAIMS_MOD


def _by_priority(items: list[TodoItem]) -> list[TodoItem]:
    return sorted(items, key=lambda item: (item.priority is None, item.priority or 0, item.row_id))


def format_mastertodo_split(items: list[TodoItem], claimed: set[str]) -> str:
    def block(title: str, rows: list[TodoItem]) -> list[str]:
        lines = [title]
        if not rows:
            lines.append("   (none)")
        for item in rows:
            priority = "?" if item.priority is None else str(item.priority)
            lines.append(f"   P{priority}  {item.row_id}  {sv._shown(item.text)}")
        return lines

    in_progress = _by_priority([item for item in items if item.row_id in claimed])
    leftover = _by_priority([item for item in items if item.row_id not in claimed])
    return "\n".join(block("in progress", in_progress) + [""] + block("leftover", leftover))


def reset_claims_cache() -> None:
    """Drop the cached claim list. Tests use this; a running page does not."""
    with _CLAIMS_LOCK:
        sv._CLAIMS_CACHE["key"] = None
        sv._CLAIMS_CACHE["at"] = 0.0
        sv._CLAIMS_CACHE["claimed"] = set()
        sv._CLAIMS_CACHE["why"] = ""


def _query_claims(cfg: Config) -> tuple[set[str], str]:
    """One call to `scripts/claims.py`, which fetches. A refusal lists nobody."""
    claims = _claims_module()
    try:
        remote = claims.claim.remote_name(cfg.remote)
        trunk = claims.trunk_name(cfg.trunk)
        _base, rows = claims.claims(cfg.repo, remote, trunk)
    except claims.Refused as exc:
        return set(), str(exc)
    found = {branch.removeprefix("seam/") for branch, _holder, _pushed, _ahead in rows
             if branch.startswith("seam/") and branch.removeprefix("seam/")}
    return found, ""


def claimed_row_ids(cfg: Config, now: float | None = None) -> tuple[set[str], str]:
    """Row ids `scripts/claims.py` lists, reused for CLAIMS_REFRESH_SECONDS.

    The page repaints far more often than the claim list changes. A hit inside
    the window does not fetch again.
    """
    now = time.time() if now is None else now
    key = (str(cfg.repo), cfg.remote, cfg.trunk or "")
    with _CLAIMS_LOCK:
        fresh = (sv._CLAIMS_CACHE["key"] == key
                 and now - float(sv._CLAIMS_CACHE["at"]) < sv.CLAIMS_REFRESH_SECONDS)
        if fresh:
            return set(sv._CLAIMS_CACHE["claimed"]), str(sv._CLAIMS_CACHE["why"])
        claimed, why = _query_claims(cfg)
        sv._CLAIMS_CACHE["key"] = key
        sv._CLAIMS_CACHE["at"] = now
        sv._CLAIMS_CACHE["claimed"] = set(claimed)
        sv._CLAIMS_CACHE["why"] = why
        return set(claimed), why


def format_mastertodo_panel(cfg: Config) -> str:
    """Claimed rows are in progress; the rest are leftover, soonest priority first.

    A refusal lists nobody: an unread remote is not an empty claim list.
    """
    items = read_todo_items(cfg.todo)
    claimed, why = claimed_row_ids(cfg)
    if why:
        return f"claims refused: {why}\nnothing is listed as in progress or leftover"
    return format_mastertodo_split(items, claimed)


def page_snapshot(cfg: Config, scanner: TranscriptScanner, sampler: OllamaSampler,
                  codex: CodexReader) -> dict[str, str]:
    """The terminal frame plus the three panels that exist only on the page."""
    frame = sv.gather(cfg, scanner, sampler, codex)
    workers = sv.read_grok_workers(cfg.grok_home)
    # Colour off, so the embedded frame matches `--once` when stdout is not a terminal.
    plain = replace(cfg, color=False)
    once = sv.render(
        plain, frame.wd, frame.sessions, frame.agents, frame.lanes, frame.ollama, frame.gpu,
        frame.worktrees, frame.todo, frame.now, frame.codex, frame.seams, frame.fetched,
        workers, sv.read_session_agents(cfg, frame.sessions, frame.agents, frame.worktrees),
        sv.read_loop_steps(cfg), ledger=frame.ledger,
    )
    return {
        "once": once,
        "claude": format_claude_panel(frame.agents),
        "codex": format_codex_panel(frame.codex, frame.now),
        "grok": sv.format_grok_panel(workers, frame.now, cfg.stale_minutes),
        "seams": format_seams_panel(cfg, frame.seams, frame.fetched, frame.now, frame.ledger),
        "progress": sv.format_progress_panel(frame.seams, sv.stream_logs(cfg)),
        "mastertodo": format_mastertodo_panel(cfg),
    }


def _stage_words(*numbers: int) -> str:
    return "|".join(re.escape(f"{n} {SEAM_STAGES[n]}") for n in numbers)


_CODEX_PHASE_BADGE = {"step": "running", "napping": "idle", "never": "idle",
                      "limit-wait": "blocked", "memory-wait": "blocked", "stopped": "stopped"}

# The page's badges: per panel, (line pattern, state). The pattern's `b` group
# is wrapped in a badge. Each is anchored where that panel's own formatter
# writes its state word, so agent-written text further along a line (a
# description, a seam's reason, a row's sentence) never becomes a badge. The
# terminal frame (`once`) has none: it is the `--once` frame byte for byte.
_PAGE_BADGES: dict[str, list[tuple[re.Pattern[str], str]]] = {
    "claude": [
        (re.compile(r"^Sub-agents (?P<b>0 running)"), "idle"),
        (re.compile(r"^Sub-agents (?P<b>[1-9]\d* running)"), "running"),
        (re.compile(r"^Sub-agents \d+ running  (?P<b>\+\d+ quiet)"), "quiet"),
        (re.compile(r"^   (?P<b>live) "), "running"),
        (re.compile(r"^   (?P<b>quiet) "), "quiet"),
    ],
    "codex": [
        *((re.compile(rf"^Codex +\S+ (?P<b>{re.escape(_PHASE_TEXT[phase])})"), state)
          for phase, state in _CODEX_PHASE_BADGE.items()),
        (re.compile(r"^   (?P<b>NEEDS OWNER:)"), "blocked"),
        (re.compile(r"^   (?P<b>STOP file set)"), "stopped"),
        (re.compile(r"^   (?P<b>blocked:)"), "blocked"),
        (re.compile(r"^(?P<b>no loop on this machine)$"), "idle"),
    ],
    "grok": [
        (re.compile(r"^(?P<b>\d+ live)$"), "running"),
        (re.compile(r"^   (?P<b>live) "), "running"),
        (re.compile(r"^   (?P<b>quiet) "), "quiet"),
        (re.compile(r"^(?P<b>no live Grok worker)$"), "idle"),
    ],
    "seams": [
        (re.compile(r"^Seams .*?(?P<b>[1-9]\d* STUCK)"), "stuck"),
        (re.compile(r"^   \S+ +(?P<b>STUCK at [1-6](?: \(fix-up\))?:)"), "stuck"),
        (re.compile(rf"^   \S+ +(?P<b>{_stage_words(1, 2, 3)})"), "running"),
        (re.compile(rf"^   \S+ +(?P<b>{_stage_words(4)})"), "ready"),
        (re.compile(rf"^   \S+ +(?P<b>{_stage_words(5, 6)})"), "idle"),
    ],
    "progress": [
        (re.compile(r"^\S+  [1-6] .*?  (?P<b>\d+% \(\d+ of \d+\))"), "running"),
        (re.compile(r"^(?P<b>\(no in-progress seam\))$"), "idle"),
    ],
    "mastertodo": [
        (re.compile(r"^(?P<b>in progress)$"), "running"),
        (re.compile(r"^(?P<b>leftover)$"), "idle"),
        (re.compile(r"^(?P<b>claims refused:)"), "blocked"),
    ],
}


def panel_html(panel: str, text: str) -> str:
    """A panel's text as page markup: all of it escaped, its own state words badged.

    Presentation only: the same text the terminal and the JSON carry, never a
    new signal. Badges are placed on the escaped text, and only fixed markup is
    added, so no agent-written text can become markup.
    """
    rules = _PAGE_BADGES.get(panel, [])
    lines: list[str] = []
    for line in html.escape(text).split("\n"):
        spans: list[tuple[int, int, str]] = []
        for pattern, state in rules:
            found = pattern.search(line)
            if found is None:
                continue
            start, end = found.span("b")
            if not any(start < e and s < end for s, e, _ in spans):
                spans.append((start, end, state))
        parts, at = [], 0
        for start, end, state in sorted(spans):
            parts += [line[at:start], f'<span class="badge badge-{state}">{line[start:end]}</span>']
            at = end
        parts.append(line[at:])
        lines.append("".join(parts))
    return "\n".join(lines)
