"""Seams: every pushed seam branch, its stage, its agent, whether trunk has it, and why it waits.
"""
from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .. import hub as sv

if TYPE_CHECKING:      # names used in annotations only (strings at runtime)
    from ..settings import Config


# ---------------------------------------------------------------- seams

# A seam's progress is a number that rises only with real progress: a fix-up
# is a tag and STUCK a flag, never a stage, so a sent-back or stalled row keeps
# the number its work earned. Where a signal is missing or unreadable the lower
# stage is shown: a wrong READY invites landing unverified work, and a wrong
# low number costs only a look.
SEAM_STAGES = {
    1: "Started",
    2: "Coding",
    3: "Coding done, verifying",
    4: "READY to land",
    5: "merged, ledger commit pending",
    6: "landed, branch left over",
}
BOARD_COLUMNS = ("In progress", "Under review", "Done", "Closed")
_STAGE_COLUMN = {1: "In progress", 2: "In progress", 3: "In progress",
                 4: "Under review", 5: "Done", 6: "Closed"}
# The state file's phase words (docs/dev/streams/W3-ledger.md § Unattended
# operation). `pushed` on a tip without both trailers is still stage 3: the
# tip, not the state file, is what makes a seam ready.
_PHASE_STAGE = {"claimed": 1, "tests-red": 1, "code": 2,
                "targeted-green": 3, "testfast-green": 3, "pushed": 3}
INTEGRATOR = "integrator"
OWNER = "owner"


@dataclass
class SeamRow:
    row_id: str
    tip: float
    ready: bool         # the tip carries `Lane:` and `Agent:` trailers
    agent: str
    lane: str
    merged: bool        # HEAD already contains the tip
    brief_on_trunk: bool
    claim_only: bool = False    # the tip is claim.py's empty `<Id>: claimed` commit
    fixup: bool = False         # the brief on HEAD carries a `revision` line
    # A ready tip the brief's revision came after: the integrator sent it back,
    # so the row is open again (docs/dev/streams/W3-ledger.md § Revision requests).
    sent_back: bool = False
    # What a stream's own files say of this row. Agent-written text: kept to be
    # shown, never acted on.
    stream: str = ""            # the stream whose state file names this row
    phase: str = ""             # that state file's `phase:`
    blocked: str = ""           # its `blocked:`, when other than none
    questions: bool = False     # its `questions:` is other than none (the owner's)
    parked: str = ""            # the parked file's question for this row
    parked_by: str = ""         # whom that line hands the row to (W1 or owner)
    idle: str = ""              # how long an unready tip has sat, once past the bound
    # The tip is already in HEAD, but this branch did not land it. That is a
    # branch cut on trunk and never advanced, or a pointer at an unrelated
    # historical commit that reached HEAD through someone else's merge. A
    # --no-ff landing of THIS row is the other case, and only that one is
    # stage 5: some merge ancestor of HEAD has this tip (or an ancestor of
    # it) as its second parent, and the merge's subject or a trailer names
    # the row.
    zero_commits: bool = False
    reason: str = ""              # why an unmerged seam is not ready to land
    review_verdict: str = ""      # latest configured review seat, when readable

    @property
    def stage(self) -> int:
        # Trailers on that trunk commit, and a state file hoping the row moved,
        # are not work on the branch. Stage 1 until the branch has a commit.
        if self.zero_commits:
            return 1
        if self.merged:
            return 5 if self.brief_on_trunk else 6
        if self.ready and not self.sent_back:
            return 4
        # A sent-back row's `pushed` is the state of the tip it replaces.
        if self.phase in _PHASE_STAGE and not (self.sent_back and self.phase == "pushed"):
            return _PHASE_STAGE[self.phase]
        return 1 if self.claim_only else 2

    @property
    def stage_name(self) -> str:
        return SEAM_STAGES[self.stage]

    @property
    def verdict(self) -> str:
        """The stage's name from 4 up; `in progress` across 1-3, the word callers key on."""
        stage = self.stage
        return SEAM_STAGES[stage] if stage >= 4 else "in progress"

    @property
    def column(self) -> str:
        return _STAGE_COLUMN[self.stage]

    @property
    def stuck(self) -> str:
        """Why the seam waits on someone, or "". Never a merged seam: its work is on HEAD."""
        if self.merged:
            return ""
        if self.review_verdict in ("HOLD", "NEEDS-OWNER", "UNKNOWN"):
            return self.reason or "unknown"
        if self.parked:
            return f"parked: {self.parked}"
        if self.blocked:
            return self.blocked
        if self.idle:
            return f"no push for {self.idle}"
        if self.reason.startswith("depends on open row "):
            return self.reason
        return ""

    @property
    def next_actor(self) -> str:
        if self.stuck:
            if self.parked:
                return self.parked_by or INTEGRATOR
            return OWNER if self.blocked and self.questions else INTEGRATOR
        if self.stage >= 4:
            return INTEGRATOR
        return self.stream or self.agent.partition("/")[0] or "developer"


def seam_order(s: SeamRow) -> tuple[int, int, float]:
    """STUCK first, then READY, then the furthest along, then the newest tip."""
    return (0 if s.stuck else 1 if s.stage == 4 else 2, -s.stage, -s.tip)


@dataclass
class StreamState:
    """One unattended stream's account of itself: its state file and its parked rows."""
    stream: str
    state: dict[str, str]
    parked: dict[str, tuple[str, str]]      # row id -> (whom it is handed to, the question)


def read_stream_state(state_dir: Path) -> StreamState | None:
    """The stream a loop's state directory serves, found as the Codex panel finds it.

    The loop log's last `loop start: <stream> in <checkout>` names both; the
    state and parked files are `<checkout>/.agent-state/<stream>.md` and
    `<stream>-parked.md`. A missing or unreadable file reads as empty, which
    marks nothing.
    """
    loop = sv.CodexLoop(state_dir)
    try:
        sv.apply_loop_log(loop, (state_dir / "loop.log").read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    if not loop.stream or loop.checkout is None:
        return None
    folder = loop.checkout / ".agent-state"
    return StreamState(loop.stream, sv.read_state_file(folder / f"{loop.stream}.md"),
                       read_parked_file(folder / f"{loop.stream}-parked.md"))


def read_parked_file(path: Path) -> dict[str, tuple[str, str]]:
    """`<Id> | <W1 or owner> | <question> | <UTC date>` lines, by row id."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    parked: dict[str, tuple[str, str]] = {}
    for line in text.splitlines():
        bits = [b.strip() for b in line.strip().strip("|").split("|")]
        if len(bits) < 3 or not bits[0]:
            continue
        question = " | ".join(bits[2:-1]) if len(bits) > 3 else bits[2]
        parked[bits[0]] = (bits[1], question)
    return parked


def _is_none(value: str) -> bool:
    return not value or value.lower().startswith("none")


def mark_seams(seams: list[SeamRow], streams: list[StreamState], now: float,
               idle_minutes: float | None) -> None:
    """What each seam's stream says of it, and an unready tip idle past the bound.

    Only a state file whose `row:` is exactly the seam's id speaks for it; a
    state file naming another row marks nothing.
    """
    for s in seams:
        for st in streams:
            if st.state.get("row", "").strip() == s.row_id:
                s.stream = st.stream
                s.phase = st.state.get("phase", "").strip()
                blocked = st.state.get("blocked", "").strip()
                if not _is_none(blocked):
                    s.blocked = blocked
                    s.questions = not _is_none(st.state.get("questions", "").strip())
            if s.row_id in st.parked:
                s.parked_by, question = st.parked[s.row_id]
                s.parked = question or "no question given"
        if idle_minutes is not None and not s.ready and not s.merged and s.tip > 0:
            idle = now - s.tip
            if idle > idle_minutes * 60:
                s.idle = sv.age(idle)


def _message_names_row(message: str, row_id: str) -> bool:
    """True when the subject, or a trailer line, names `row_id` as a whole token.

    A shorter id must not match a longer one (`BS-M` is not `BS-MERGED`).
    Trailers are the last paragraph's `Key: value` lines, the same place
    `Lane:` and `Agent:` sit.
    """
    if not row_id:
        return False
    pat = re.compile(rf"(?<![A-Za-z0-9-]){re.escape(row_id)}(?![A-Za-z0-9-])")
    lines = message.splitlines()
    if lines and pat.search(lines[0]):
        return True
    paragraphs = [p for p in message.strip().split("\n\n") if p.strip()]
    if not paragraphs:
        return False
    for line in paragraphs[-1].splitlines():
        key, sep, _value = line.partition(":")
        if sep and key and " " not in key and key[:1].isalpha() and pat.search(line):
            return True
    return False


def _head_merges(cfg: Config) -> list[tuple[str, str]]:
    """`(second parent, message)` for every merge commit reachable from HEAD.

    An unreadable history is empty, which cannot prove a landing: the caller
    then leaves an ancestor at stage 1.
    """
    raw = sv._run(["git", "log", "--merges", "-z", "--format=%H%x1f%P%x1f%B", "HEAD"],
               cwd=cfg.repo)
    found: list[tuple[str, str]] = []
    for record in raw.split("\0"):
        if not record.strip():
            continue
        parts = record.split("\x1f", 2)
        if len(parts) != 3:
            continue
        _sha, parents, message = parts
        parent_list = parents.split()
        if len(parent_list) < 2:
            continue
        found.append((parent_list[1], message))
    return found


def _second_parent_covers_tip(cfg: Config, second: str, tip: str) -> bool:
    """The merge's second parent is the seam tip, or an ancestor of it."""
    if second == tip:
        return True
    return _git_ok(["git", "merge-base", "--is-ancestor", second, tip], cfg.repo)


def _row_landed(cfg: Config, row_id: str, tip: str, merges: list[tuple[str, str]]) -> bool:
    """HEAD contains a --no-ff merge of this row, not merely some path to the tip."""
    for second, message in merges:
        if _message_names_row(message, row_id) and _second_parent_covers_tip(cfg, second, tip):
            return True
    return False


def parse_trailers(message: str) -> tuple[str, str]:
    """(Lane:, Agent:) trailer values of a commit message; empty when absent."""
    lane = agent = ""
    for line in message.splitlines():
        if line.startswith("Lane:"):
            lane = line[len("Lane:"):].strip()
        elif line.startswith("Agent:"):
            agent = line[len("Agent:"):].strip()
    return lane, agent


def seam_reason(seam: SeamRow, brief: str, dependency_open) -> str:
    """First structural reason a pushed seam cannot be called ready to land."""
    if not seam.brief_on_trunk:
        return "brief missing from trunk"
    if seam.sent_back:
        return "integrator revision awaits a fresh tip"
    if seam.zero_commits:
        return "claim has no row commit"
    if not seam.ready:
        return "missing Lane or Agent trailer"
    for line in brief.splitlines():
        if not line.startswith("depends on:"):
            continue
        for dependency in re.findall(r"\b[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*\b", line.partition(":")[2]):
            if dependency_open(dependency):
                return f"depends on open row {dependency}"
        break
    return ""


def read_seams(cfg: Config, now: float | None = None) -> tuple[list[SeamRow], float | None]:
    """Pushed seam branches as the last fetch saw them, and when that fetch was."""
    prefix = f"refs/remotes/{cfg.remote}/seam/"
    out = sv._run(["git", "for-each-ref", "--format=%(refname)%09%(objectname)%09%(committerdate:unix)",
                prefix], cwd=cfg.repo)
    rows: list[SeamRow] = []
    # Trunk's first-parent spine. An unreadable spine cannot prove a tip
    # arrived through a merge, and a wrong "merged" is the costly answer, so
    # an ancestor then stays stage 1. A readable spine still is not enough:
    # a tip reached only through someone else's merge is an ancestor and is
    # not on the spine, and that is not this row landing.
    spine = sv._run(["git", "rev-list", "--first-parent", "HEAD"], cwd=cfg.repo)
    merges = _head_merges(cfg) if spine else []
    for line in out.splitlines():
        bits = line.split("\t")
        if len(bits) != 3:
            continue
        ref, sha, when = bits
        row_id = ref.removeprefix(prefix)
        message = sv._run(["git", "log", "-1", "--format=%B", sha], cwd=cfg.repo)
        lane, agent = parse_trailers(message)
        subject = message.strip().splitlines()[0].strip() if message.strip() else ""
        try:
            tip = float(when)
        except ValueError:
            tip = 0.0
        on_trunk = _git_ok(["git", "cat-file", "-e", f"HEAD:todo/{row_id}.md"], cfg.repo)
        brief = sv._run(["git", "show", f"HEAD:todo/{row_id}.md"], cwd=cfg.repo) if on_trunk else ""
        fixup = any(ln.startswith("revision") for ln in brief.splitlines())
        sent_back = False
        if fixup and lane and agent:
            # The brief's last change on HEAD against the tip; unreadable reads
            # as sent back, the lower stage.
            changed = sv._run(["git", "log", "-1", "--format=%ct", "HEAD", "--", f"todo/{row_id}.md"],
                           cwd=cfg.repo)
            try:
                sent_back = float(changed) >= tip
            except ValueError:
                sent_back = True
        ancestor = _git_ok(["git", "merge-base", "--is-ancestor", sha, "HEAD"], cfg.repo)
        # No readable spine, or the tip is not in HEAD: do not call it merged.
        # An ancestor with no merge of THIS row is a zero-commit claim.
        landed = bool(spine) and ancestor and _row_landed(cfg, row_id, sha, merges)
        seam = SeamRow(
            row_id, tip, bool(lane and agent), agent, lane,
            landed,
            on_trunk,
            claim_only=subject == f"{row_id}: claimed",
            fixup=fixup,
            sent_back=sent_back,
            zero_commits=ancestor and not landed,
        )
        if not landed:
            seam.reason = seam_reason(
                seam, brief,
                lambda dependency: _git_ok(["git", "cat-file", "-e", f"HEAD:todo/{dependency}.md"],
                                            cfg.repo),
            )
        rows.append(seam)
    streams = [st for d in cfg.stream_state if (st := read_stream_state(d)) is not None]
    mark_seams(rows, streams, time.time() if now is None else now, cfg.seam_idle_minutes)
    try:
        fetched = (cfg.git_dir / "FETCH_HEAD").stat().st_mtime
    except OSError:
        fetched = None
    rows.sort(key=seam_order)
    return rows, fetched


def _git_ok(cmd: list[str], cwd: Path) -> bool:
    try:
        return subprocess.run(cmd, cwd=str(cwd), capture_output=True, timeout=10.0,
                              check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
