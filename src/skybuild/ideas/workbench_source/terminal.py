"""The terminal view: painting, trimming, each panel's text, the main loop and its options. `render`
is what `sessionview-console` prints and what the legacy page's `#once` frame shows.
"""
from __future__ import annotations

import importlib.util
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from . import hub as sv

if TYPE_CHECKING:      # names used in annotations only (strings at runtime)
    from .frame import GrokWorker
    from .settings import Config
    from .sources.codex_loop import CodexLoop, CodexReader
    from .sources.lanes import LaneRow
    from .sources.ollama import GpuInfo, OllamaRow, OllamaSampler
    from .sources.seams import SeamRow
    from .sources.sessions import AgentRow, Session, TranscriptScanner
    from .sources.subagents import LoopSteps, SessionAgents
    from .sources.todo_file import TodoCounts
    from .sources.worktrees import WorktreeRow
    from .util import Watchdog


# ---------------------------------------------------------------- rendering

class Paint:
    def __init__(self, enabled: bool) -> None:
        self.on = enabled

    def __call__(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.on else text

    def green(self, t): return self(t, "32")
    def red(self, t): return self(t, "31")
    def yellow(self, t): return self(t, "33")
    def cyan(self, t): return self(t, "36")
    def dim(self, t): return self(t, "2")
    def bold(self, t): return self(t, "1")


def _visible_len(text: str) -> int:
    return len(re.sub(r"\033\[[0-9;]*m", "", text))


def trim(text: str, width: int) -> str:
    """Truncate to a visible width, leaving any colour codes closed."""
    if _visible_len(text) <= width:
        return text
    out, count = [], 0
    i = 0
    while i < len(text) and count < width - 1:
        if text[i] == "\033":
            j = text.find("m", i)
            if j == -1:
                break
            out.append(text[i:j + 1])
            i = j + 1
            continue
        out.append(text[i])
        count += 1
        i += 1
    trimmed = "".join(out) + "…"
    return trimmed + "\033[0m" if "\033" in trimmed else trimmed


_PHASE_TEXT = {
    "step": "working a step",
    "napping": "between steps",
    "limit-wait": "waiting for a usage window to reset",
    "memory-wait": "waiting for memory",
    "stopped": "STOPPED",
    "never": "no step logged yet",
}


def render_codex(c: Paint, loop: CodexLoop, width: int, now: float) -> list[str]:
    """The Codex loop block: liveness and phase, the row, the step in flight, the last outcomes."""
    stream = loop.stream or "?"
    since = f" {sv.age(now - loop.since)}" if loop.since else ""
    phase = _PHASE_TEXT.get(loop.phase, loop.phase) + since
    if loop.phase == "stopped":
        head = c.red(phase)
    elif loop.phase == "step":
        head = c.green(phase)
    elif loop.phase in ("limit-wait", "memory-wait"):
        head = c.yellow(phase)
    else:
        head = c.dim(phase)
    if loop.note and loop.phase != "step":
        head += c.dim(f" ({loop.note})")
    model = loop.step.model if loop.step else None
    extras = [f"model {model}"] if model else []
    extras += [f"{label} {pct:.0f}%" for label, pct in loop.limits]
    if loop.running:
        extras.append("pid " + ",".join(str(p) for p in loop.pids))
    tail = c.dim("  · " + " · ".join(extras)) if extras else ""
    lines = [trim(f"{c.bold('Codex')}      {c.cyan(stream)} {head}{tail}", width)]

    if loop.needs_owner:
        lines.append(trim("   " + c.red("NEEDS OWNER: ") + loop.needs_owner, width))
    if loop.stop_file:
        lines.append(c.yellow(f"   STOP file set — the loop exits after this step ({loop.state_dir / 'STOP'})"))
    if loop.state:
        st = loop.state
        fresh = c.dim(f" · state {sv.age(loop.state_age)} ago") if loop.state_age is not None else ""
        lines.append(trim(f"   row {c.cyan(st.get('row', '?'))} · {st.get('kind', '?')}"
                          f" · phase {st.get('phase', '?')}{fresh}", width))
        blocked = st.get("blocked", "")
        if blocked and not blocked.lower().startswith("none"):
            lines.append(trim("   " + c.yellow("blocked: ") + blocked, width))
        elif st.get("next"):
            lines.append(trim(c.dim("   next: ") + st["next"], width))
    if loop.step and loop.step.doing:
        step = loop.step
        label = "now" if loop.phase == "step" else "last step"
        count = c.dim(f" · {step.commands} commands") if step.commands else ""
        lines.append(trim(f"   {label}: {step.doing}{count}", width))
        if step.tokens and loop.phase != "step":
            lines.append(trim(c.dim(f"   last step used {step.tokens}"), width))
    if loop.recent:
        parts = [f"{sv.local(ts):%H:%M} {status} {detail}".strip() for ts, status, detail in loop.recent]
        lines.append(trim(c.dim("   recent: " + " · ".join(parts)), width))
    return lines


def _shown(value: str) -> str:
    """Agent-written text made safe to print: control characters dropped, whitespace collapsed."""
    return " ".join("".join(ch if ch.isprintable() else " " for ch in value).split())


def render_seams(c: Paint, seams: list[SeamRow], fetched: float | None, width: int,
                 now: float, idle_minutes: float | None = None) -> list[str]:
    """One line per seam, its stage first; the header counts each board column.

    A STUCK seam is counted as STUCK rather than in its column, so the counts
    add up to the seams pushed.
    """
    when = c.dim(f" (as of the fetch {sv.age(now - fetched)} ago)") if fetched else c.dim(" (never fetched)")
    parts = []
    for col in sv.BOARD_COLUMNS:
        n = sum(1 for s in seams if not s.stuck and s.column == col)
        text = f"{n} {col}"
        parts.append(c.green(text) if n and col == "Under review" else c.dim(text))
    stuck = sum(1 for s in seams if s.stuck)
    parts.append(c.red(f"{stuck} STUCK") if stuck else c.dim("0 STUCK"))
    lines = [trim(f"{c.bold('Seams')}      {len(seams)} pushed · " + " · ".join(parts) + when, width)]
    bound = (f"idle past {sv.age(idle_minutes * 60)}" if idle_minutes is not None
             else "idle (no bound set)")
    legend = " · ".join(f"{n} {name}" for n, name in sv.SEAM_STAGES.items())
    lines.append(trim(c.dim(f"   {legend} · fix-up: sent back · STUCK: parked, blocked or {bound}"),
                      width))
    for s in sorted(seams, key=sv.seam_order):
        who = _shown(s.agent) or "no Agent: trailer"
        lane = c.dim(" · " + _shown(s.lane)) if s.lane else ""
        tip = c.dim(f" · tip {sv.age(now - s.tip)} ago")
        actor = _shown(s.next_actor)
        tag = " (fix-up)" if s.fixup and s.stage <= 3 else ""
        if s.stuck:
            # The reason is cut to fit, never the next actor: who must act is
            # the point of the flag.
            prefix = f"   {s.row_id:<28} " + c.red(f"STUCK at {s.stage}{tag}: ")
            suffix = f"   next: {c.bold(actor)}"
            reason = _shown(s.stuck)
            if _visible_len(prefix + reason + suffix) < width:
                lines.append(trim(prefix + reason + suffix + f"   {who}{lane}{tip}", width))
            else:
                room = width - _visible_len(prefix) - _visible_len(suffix)
                lines.append(trim(prefix + (trim(reason, room) if room > 1 else "") + suffix, width))
            continue
        plain = f"{s.stage} {s.stage_name}"
        paint = c.green if s.stage == 4 else c.yellow if s.stage >= 5 else c.dim
        stage = paint(plain) + c.yellow(tag) + " " * max(0, 34 - len(plain + tag))
        lines.append(trim(f"   {s.row_id:<28} {stage} {s.column:<12}  next: {actor:<10}  "
                          f"{who}{lane}{tip}", width))
    return lines


_SEAM_MERGE_MOD = None


def _seam_merge_module():
    """`scripts/seam_merge.py`, loaded from beside this file: its count is the one count."""
    global _SEAM_MERGE_MOD
    if _SEAM_MERGE_MOD is None:
        spec = importlib.util.spec_from_file_location(
            "sessionview_seam_merge", sv.SCRIPTS / "seam_merge.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _SEAM_MERGE_MOD = module
    return _SEAM_MERGE_MOD


@dataclass(frozen=True)
class LedgerDebtRow:
    """The merges waiting on a gate and a ledger commit, as `seam_merge.py` counts them."""
    merges: int | None       # None: HEAD's first-parent history could not be read
    text: str                # seam_merge's own sentence for the count; "" when unreadable
    limit: int | None        # None: no limit was set, or the one set is not a count
    limit_why: str = ""      # seam_merge's own reason when `limit` is None


def read_ledger_debt(cfg: Config) -> LedgerDebtRow | None:
    """The merges on HEAD's first-parent history above the newest ledger commit.

    Counted and worded by `scripts/seam_merge.py`, so the view cannot disagree
    with the refusal. A repository whose history git cannot read is unreadable,
    never zero. None only where there is no repository at all: no trunk owes
    anything there, and the frame is unchanged.
    """
    if not sv._git_ok(["git", "rev-parse", "--git-dir"], cfg.repo):
        return None
    merge = _seam_merge_module()
    limit, why = merge.merge_limit(cfg.max_unledgered_merges)
    spine = sv._run(["git", *merge.SPINE_LOG], cwd=cfg.repo)
    if not spine.strip():
        return LedgerDebtRow(None, "", limit, why)
    try:
        debt = merge.ledger_debt(spine)
    except ValueError:
        return LedgerDebtRow(None, "", limit, why)
    return LedgerDebtRow(debt.merges, merge.debt_text(debt), limit, why)


def render_ledger_debt(c: Paint, row: LedgerDebtRow, width: int) -> str:
    """The one line beside the Seams panel; red once `seam_merge.py` would refuse."""
    label = c.bold("Ledger") + "     "
    if row.merges is None:
        return trim(label + c.yellow("merges above the last ledger commit: unreadable "
                                     "(git could not read the first-parent history of HEAD)"),
                    width)
    if row.limit is None:
        return trim(label + c.red(f"{row.text} · seam_merge.py refuses every merge: "
                                  f"{_shown(row.limit_why)}"), width)
    if row.merges > row.limit:
        return trim(label + c.red(f"{row.text} · over the limit of {row.limit}: seam_merge.py "
                                  "refuses until the batch is gated and its ledger committed"),
                    width)
    return trim(label + row.text + c.dim(f" · limit {row.limit}"), width)


def _lane_owner(lane: LaneRow, sessions: list[Session], codex: CodexLoop | None) -> str:
    if lane.session_pid:
        sid = next((x.session_id for x in sessions if x.pid == lane.session_pid), None)
        return f"session {sid[:8]}" if sid else f"pid {lane.session_pid}"
    # Its launching shell exited, so the chain to a session is gone — but the
    # checkout it runs in still says whose it is.
    if lane.cwd and codex and codex.checkout and (
            lane.cwd == str(codex.checkout) or lane.cwd.startswith(str(codex.checkout) + "/")):
        return f"codex {codex.stream or ''}".strip()
    return f"detached, in {Path(lane.cwd).name}" if lane.cwd else "detached"


def render(cfg: Config, wd: Watchdog, sessions: list[Session], agents: list[AgentRow],
           lanes: list[LaneRow], ollama: list[OllamaRow], gpu: GpuInfo | None,
           worktrees: list[WorktreeRow], todo: TodoCounts, now: float,
           codex: CodexLoop | None = None, seams: list[SeamRow] | None = None,
           fetched: float | None = None, grok: list[GrokWorker] | None = None,
           by_session: list[SessionAgents] | None = None, loop_steps: LoopSteps | None = None,
           ledger: LedgerDebtRow | None = None) -> str:
    busy = {a.worktree: a.state for a in agents if a.worktree and a.state != "done"}
    for w in worktrees:
        w.agent_state = busy.get(w.path.name)
    c = Paint(cfg.color)
    size = shutil.get_terminal_size((100, 40))
    width = max(60, size.columns)
    lines: list[str] = []

    running = [a for a in agents if a.state == "running"]
    stale = [a for a in agents if a.state == "stale"]
    mapped = [s for s in sessions if s.session_id]

    head = c.bold("sessionview") + c.dim(f"  {cfg.repo}")
    lines.append(head)
    lines.append(c.dim(f"{sv.local(now):%Y-%m-%d %H:%M:%S}  ·  refresh {cfg.interval:.0f}s  ·  q or Ctrl-C to quit"))
    lines.append("")

    # --- watchdog ------------------------------------------------------------
    if wd.running:
        tick = ""
        if wd.log_age is not None:
            quiet = wd.log_age > 3600
            tick = c.dim("  last log ") + (c.yellow(sv.age(wd.log_age) + " ago") if quiet
                                           else c.dim(sv.age(wd.log_age) + " ago"))
        pids = ",".join(str(p) for p in wd.pids)
        lines.append(f"{c.bold('Watchdog')}   {c.green('RUNNING')} {c.dim('pid ' + pids)}{tick}")
    else:
        lines.append(f"{c.bold('Watchdog')}   {c.red('NOT RUNNING')} "
                     f"{c.dim('(pattern: ' + cfg.watchdog_pattern + ')')}")

    # --- sessions + agents ---------------------------------------------------
    unmapped = len(sessions) - len(mapped)
    sess_note = f"{len(mapped)} session{'s' if len(mapped) != 1 else ''}"
    if mapped:
        sess_note += c.dim("  " + "  ".join(
            f"{s.session_id[:8]} (pid {s.pid}, up {sv.age(now - s.started)})" for s in mapped))
    if unmapped:
        sess_note += c.dim(f"  +{unmapped} unidentified")
    lines.append(f"{c.bold('Sessions')}   " + trim(sess_note, width - 12))

    count = c.green(str(len(running))) if running else c.dim("0")
    extra = c.yellow(f"  +{len(stale)} quiet >{cfg.stale_minutes:.0f}m") if stale else ""
    lines.append(f"{c.bold('Sub-agents')} {count} running{extra}")
    for a in running + stale:
        flag = c.yellow("quiet") if a.state == "stale" else c.green("live ")
        wt = c.dim(f" [{a.worktree}]") if a.worktree else ""
        lines.append(trim(
            f"   {flag} {c.cyan(a.model):<12} {a.description}"
            f"{wt} {c.dim('· ' + sv.age(now - a.last_write) + ' since last write')}", width))
    if not running and not stale:
        lines.append(c.dim("   (none — no sub-agent is working right now)"))

    # A live Grok worker, same line the page prints. No worker adds nothing:
    # the frame stays byte-for-byte what it was.
    if grok:
        lines.append("")
        lines.append(f"{c.bold('Grok')}       {len(grok)} live")
        for worker in grok:
            lines.append(trim(sv._grok_worker_line(worker, now, cfg.stale_minutes), width))

    # --- sub-agents by session ------------------------------------------------
    # Each live session with its own sub-agents under it. With no session live
    # there is nothing to list under one, and the frame stays byte for byte.
    if by_session:
        lines.append("")
        lines.extend(sv.render_session_agents(c, by_session, loop_steps, width, now))

    # --- work in flight ------------------------------------------------------
    # Sub-agents are not the only thing that works: the integrating session runs
    # its own lanes. Without this block a busy session reads as an idle one.
    lines.append("")
    if lanes:
        lines.append(f"{c.bold('Work')}       {len(lanes)} lane{'s' if len(lanes) != 1 else ''} in flight")
        for lane in lanes:
            owner = c.dim(f"  ({_lane_owner(lane, sessions, codex)})")
            lines.append(trim(f"   {c.cyan(lane.kind):<12} {lane.detail}"
                              f" {c.dim('· ' + sv.age(now - lane.started))}{owner}", width))
    else:
        lines.append(f"{c.bold('Work')}       {c.dim('no test lane, compose or migration running')}")

    # --- ollama --------------------------------------------------------------
    if ollama or gpu:
        busy = [o for o in ollama if o.busy]
        head = f"{len(ollama)} instance{'s' if len(ollama) != 1 else ''}"
        head += c.green(f" · {len(busy)} busy") if busy else c.dim(" · none busy")
        if gpu:
            gtext = f" · GPU {gpu.util}% {gpu.used}/{gpu.total} MiB"
            head += c.yellow(gtext) if gpu.util > 50 else c.dim(gtext)
        lines.append(f"{c.bold('Ollama')}     {head}")
        for o in ollama:
            state = c.green("BUSY") if o.busy else c.dim("idle")
            resident = o.models or (f"{o.resident} model(s) resident" if o.resident
                                    else c.dim("no model resident"))
            lines.append(trim(f"   {o.label:<16} {resident}  {state}"
                              f" {c.dim(f'{o.cpu_pct:.0f}% cpu')}", width))

    # --- codex loop + seams --------------------------------------------------
    # The Codex stream works outside every session above, and the seams are
    # where every stream's work waits to be landed.
    if codex:
        lines.append("")
        lines.extend(render_codex(c, codex, width, now))
    if seams:
        lines.append("")
        lines.extend(render_seams(c, seams, fetched, width, now, cfg.seam_idle_minutes))
    # The merges no ledger commit has answered for, directly under the seams
    # that are waiting to join them. Handed none, the frame is unchanged.
    if ledger is not None:
        if not seams:
            lines.append("")
        lines.append(render_ledger_debt(c, ledger, width))

    # --- worktrees -----------------------------------------------------------
    lines.append("")
    dirty_total = sum(1 for w in worktrees if w.dirty)
    note = c.dim(f" · {dirty_total} with uncommitted work") if dirty_total else ""
    lines.append(f"{c.bold('Worktrees')}  {len(worktrees)} open{note}")

    # Keep the frame inside the terminal: the header block, the task block and
    # a little slack for the footer are reserved, the rest goes to worktrees.
    reserved = len(lines) + 8
    room = max(3, size.lines - reserved)
    shown = sorted(worktrees, key=lambda w: (w.agent_state is None, -w.latest))
    for w in shown[:room]:
        kind = "commit " if w.latest_kind == "commit" else "created"
        dirty = ""
        if w.dirty:
            dirty = c.yellow(f"  {w.dirty} uncommitted")
        elif w.dirty == 0:
            dirty = c.dim("  clean")
        agent_mark = ""
        if w.agent_state == "running":
            agent_mark = c.green("  ← agent working")
        elif w.agent_state == "stale":
            agent_mark = c.yellow("  ← agent quiet")
        lines.append(trim(
            f"   {w.path.name:<34} {c.dim(kind)} {sv.stamp(w.latest)} "
            f"{c.dim('(' + sv.age(now - w.latest) + ' ago)')}{dirty}{agent_mark}", width))
    if len(shown) > room:
        lines.append(c.dim(f"   … {len(shown) - room} older not shown (terminal height)"))
    if not shown:
        lines.append(c.dim("   (none)"))

    # --- to-do ---------------------------------------------------------------
    lines.append("")
    if todo.error:
        lines.append(f"{c.bold('Tasks')}      {c.red(todo.error)}")
    else:
        # A pushed seam branch is the claim (docs/dev/streams/README.md), so a
        # row with one is in process whatever the file says.
        listed = set(todo.ids)
        claimed = [s.row_id for s in (seams or []) if s.row_id in listed
                   and s.row_id not in todo.ids_in_process]
        in_process = todo.ids_in_process + claimed
        parts = []
        for name, _ in sv.STATE_BUCKETS + [("open", ()), ("other", ())]:
            n = todo.buckets.get(name, 0)
            if not n:
                continue
            text = f"{n} {name}"
            parts.append(c.yellow(text) if name == "in process" else c.dim(text))
        if claimed:
            parts.append(c.yellow(f"{len(claimed)} claimed by a pushed seam"))
        summary = "   " + " · ".join(parts) if parts else ""
        lines.append(f"{c.bold('Tasks')}      {todo.total} in {cfg.todo.name}{summary}")
        if in_process:
            lines.append(trim(c.dim("   in process: ") + ", ".join(in_process), width))

    return "\n".join(lines)


# ---------------------------------------------------------------- main loop

def collect_and_render(cfg: Config, scanner: TranscriptScanner,
                      sampler: OllamaSampler, codex: CodexReader) -> str:
    frame = sv.gather(cfg, scanner, sampler, codex)
    return sv.render(
        cfg, frame.wd, frame.sessions, frame.agents, frame.lanes, frame.ollama, frame.gpu,
        frame.worktrees, frame.todo, frame.now, frame.codex, frame.seams, frame.fetched,
        sv.read_grok_workers(cfg.grok_home),
        sv.read_session_agents(cfg, frame.sessions, frame.agents, frame.worktrees),
        sv.read_loop_steps(cfg), ledger=frame.ledger,
    )


def legacy_main(argv: list[str] | None = None) -> int:
    cfg = sv.legacy_resolve_config(argv)
    if cfg.serve:
        return sv.serve(cfg)
    scanner = sv.TranscriptScanner()
    sampler = sv.OllamaSampler()
    codex = sv.CodexReader()

    if cfg.once or not sys.stdout.isatty():
        print(collect_and_render(cfg, scanner, sampler, codex))
        return 0

    import select
    import signal
    import termios
    import tty

    # A terminal left in the alternate screen with a hidden cursor is a wrecked
    # terminal, so every ordinary way of being told to stop has to reach the
    # restore in `finally` — SIGTERM included, which by default would not.
    def _stop(_signum, _frame):
        raise KeyboardInterrupt

    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, _stop)
        except (OSError, ValueError):
            pass

    fd = sys.stdin.fileno() if sys.stdin.isatty() else None
    saved = termios.tcgetattr(fd) if fd is not None else None
    sys.stdout.write("\033[?1049h\033[?25l")            # alt screen, hide cursor
    try:
        if fd is not None:
            tty.setcbreak(fd)
        while True:
            frame = collect_and_render(cfg, scanner, sampler, codex)
            sys.stdout.write("\033[H\033[2J" + frame + "\n")
            sys.stdout.flush()
            deadline = time.time() + cfg.interval
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                if fd is None:
                    time.sleep(min(remaining, 0.5))
                    continue
                ready, _, _ = select.select([sys.stdin], [], [], min(remaining, 0.5))
                if ready and sys.stdin.read(1).lower() in ("q", "\x03"):
                    return 0
    except KeyboardInterrupt:
        return 0
    finally:
        if fd is not None and saved is not None:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        sys.stdout.write("\033[?25h\033[?1049l")        # cursor back, main screen
        sys.stdout.flush()


ENV = "SESSIONVIEW_WEB_"
PORT_VAR = ENV + "PORT"
LOOPBACK = "127.0.0.1"
#: Unset SESSIONVIEW_WEB_PORT falls back to this (dev tool, dev box only); the env still overrides.
DEFAULT_PORT = 18437

#: The testlong stack's published ports (AGENTS.md § Shared checkout): left up
#: on purpose, so this server must never take one of them.
TESTLONG_PORTS = (8790, 8794)
