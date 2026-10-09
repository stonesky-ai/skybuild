"""One frame of the terminal view: `gather` runs every collector once, and the stream, seam-estimate
and progress panels read from it.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from . import hub as sv

if TYPE_CHECKING:      # names used in annotations only (strings at runtime)
    from .settings import Config
    from .sources.codex_loop import CodexLoop, CodexReader
    from .sources.ollama import GpuInfo, OllamaSampler
    from .sources.seams import SeamRow, StreamState
    from .sources.sessions import TranscriptScanner
    from .sources.todo_file import TodoCounts
    from .terminal import LedgerDebtRow
    from .util import Watchdog


# ---------------------------------------------------------------- page-only panels

# How often the MasterToDo split may call scripts/claims.py. The page repaints
# on its own interval; a fetch on that cadence would hammer the remote.
CLAIMS_REFRESH_SECONDS = 60.0


_CLAIMS_CACHE: dict = {"key": None, "at": 0.0, "claimed": set(), "why": ""}


@dataclass
class Frame:
    now: float
    wd: Watchdog
    sessions: list
    agents: list
    lanes: list
    ollama: list
    gpu: GpuInfo | None
    worktrees: list
    todo: TodoCounts
    codex: CodexLoop | None
    seams: list
    fetched: float | None
    ledger: LedgerDebtRow | None = None


@dataclass(frozen=True)
class SeamEstimate:
    percent: str
    done: int
    total: int
    eta: str


@dataclass(frozen=True)
class GrokWorker:
    model: str
    description: str
    worktree: str              # the session's cwd, decoded from its sessions/ directory name
    session_id: str = ""
    pid: int = 0
    last_seen: float = 0.0     # epoch of the newest log line of this session's process


@dataclass(frozen=True)
class TodoItem:
    row_id: str
    priority: int | None
    text: str


def gather(cfg: Config, scanner: TranscriptScanner, sampler: OllamaSampler,
           codex: CodexReader) -> Frame:
    """One read of every signal the terminal frame is built from, in the same order."""
    now = time.time()
    sessions = sv.live_sessions(cfg)
    ollama, gpu = sampler.read(now, prime=True)
    seams, fetched = sv.read_seams(cfg, now)
    return Frame(
        now, sv.read_watchdog(cfg), sessions, sv.read_agents(cfg, sessions, scanner, now),
        sv.read_lanes(sessions), ollama, gpu, sv.read_worktrees(cfg, cfg.show_dirty),
        sv.read_todo(cfg.todo), codex.read(cfg, now), seams, fetched,
        sv.read_ledger_debt(cfg),
    )


def stream_iteration_durations(log_text: str) -> list[float]:
    """Seconds between each iteration start and its end in one stream's loop log."""
    pending: float | None = None
    durations: list[float] = []
    for line in log_text.splitlines():
        matched = sv._LOG_LINE.match(line)
        if not matched:
            continue
        try:
            ts = datetime.strptime(matched.group(1), "%Y-%m-%d %H:%M:%S").astimezone().timestamp()
        except ValueError:
            continue
        msg = matched.group(2)
        if sv._ITER_START.match(msg):
            pending = ts
            continue
        if sv._ITER_END.match(msg) is None or pending is None:
            continue
        durations.append(max(0.0, ts - pending))
        pending = None
    return durations


def estimate_seam(seam: SeamRow, state: dict[str, str], log_text: str) -> SeamEstimate | None:
    """Percent and ETA for an in-progress seam.

    The percent is the seam's own stage out of the six the Seams panel already
    shows (1 started … 6 landed). `state` is not a second scale. The ETA is the
    stages still to go times the mean iteration length in that stream's log.
    A seam that is no longer in progress has neither.
    """
    del state
    if seam.verdict != "in progress":
        return None
    done, total = seam.stage, len(sv.SEAM_STAGES)
    durations = stream_iteration_durations(log_text)
    eta = ("ETA " + sv.age((sum(durations) / len(durations)) * (total - done))) if durations else ""
    return SeamEstimate(f"{done * 100 // total}%", done, total, eta)


def stream_logs(cfg: Config) -> list[tuple[StreamState, str]]:
    """Each stream's state paired with the loop log the ETA is timed from."""
    paired: list[tuple[StreamState, str]] = []
    for directory in cfg.stream_state:
        state = sv.read_stream_state(directory)
        if state is None:
            continue
        try:
            text = (directory / "loop.log").read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        paired.append((state, text))
    return paired


def format_progress_panel(seams: list[SeamRow], streams: list[tuple[StreamState, str]]) -> str:
    lines: list[str] = []
    for seam in sorted(seams, key=sv.seam_order):
        if seam.verdict != "in progress":
            continue
        state: dict[str, str] = {}
        log = ""
        for stream, text in streams:
            if stream.state.get("row", "").strip() == seam.row_id:
                state, log = stream.state, text
        estimate = estimate_seam(seam, state, log)
        if estimate is None:
            continue
        eta = f"  {estimate.eta}" if estimate.eta else ""
        lines.append(
            f"{seam.row_id}  {seam.stage} {seam.stage_name}  "
            f"{estimate.percent} ({estimate.done} of {estimate.total}){eta}"
        )
    return "\n".join(lines) if lines else "(no in-progress seam)"
