"""Lanes live on this box, the gate endpoint, and the review seats: what a reviewer held and why.
"""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import TYPE_CHECKING

from .. import hub as sv

if TYPE_CHECKING:      # names used in annotations only (strings at runtime)
    from ..sections.work import Runner


#: The lane processes that need the model endpoint while they run. A lane is
#: matched on its own argv, read from /proc, and never with a `pgrep -f`
#: pattern: such a pattern matches the matching command's own argv too, which
#: on 2026-09-30 made this box's gate look live for 16 minutes after it ended.
LANE_ARGV_MARKS = ("gate_run.py", "gate_lane.sh", "pytest")
#: A mark is only a lane when it is what the process RUNS, so an argument is
#: matched whole (`pytest`, `.../gate_lane.sh`) and never as a substring: a
#: `grep pytest` carries the word and runs no test. These commands carry other
#: people's arguments for a living and are never a lane whatever they hold.
NOT_A_LANE = {"grep", "egrep", "fgrep", "rg", "ripgrep", "sed", "awk", "tr", "cut", "cat",
              "tail", "head", "less", "more", "find", "xargs", "ps", "pgrep", "pkill", "rtk"}


def lanes_live(skip: set[int] | None = None) -> list[str]:
    """The live lane processes' names, empty when none is running.

    Read from /proc/<pid>/cmdline, never with a `pgrep -f` pattern: such a
    pattern matches the matching command's own argv, which on 2026-09-30 made
    this box's gate read as live for 16 minutes after it had finished. This
    server's own argv cannot match either — it is excluded by pid, with its
    parent and anything the caller names.
    """
    mine = {os.getpid(), os.getppid()} | (skip or set())
    found: list[str] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) in mine:
            continue
        try:
            argv = [part for part in
                    (entry / "cmdline").read_bytes().decode("utf-8", "replace").split("\0") if part]
        except OSError:
            continue
        if not argv or os.path.basename(argv[0]) in NOT_A_LANE:
            continue
        if not any(os.path.basename(part) == mark for part in argv for mark in LANE_ARGV_MARKS):
            continue
        found.append(f"{entry.name} {sv._shown(' '.join(argv))[:120]}")
    return sorted(found)


def gate_endpoint(conf: Path | None) -> str:
    """The model endpoint the lane configuration names, "" when none is read.

    Only the base-URL assignment is read. Everything else in that file stays
    unread and unreported: it names the endpoint's credential file, and this
    page prints no secret and no path to one.
    """
    if conf is None:
        return ""
    try:
        text = conf.read_text()
    except OSError:
        return ""
    found = ""
    for line in text.splitlines():
        match = re.match(r"\s*(?:export\s+)?SKYKEEP_GATE_MODEL_BASE_URL=(.*)$", line)
        if match:
            found = match.group(1).strip().strip('"').strip("'")
    return found


#: The verdicts a review seat file can carry on its first line. A seam whose
#: review says HOLD or NEEDS-OWNER is waiting for a PERSON, not for a lane, so
#: it does not keep a metered endpoint alive.
HELD_VERDICTS = ("HOLD", "NEEDS-OWNER")


#: The line a review seat file carries to say which tip it judged:
#: `Reviewed: <sha>`, 7 to 64 hex digits, on any line after the verdict. A seat
#: file with no such line, or one whose value is not a sha, names no tip and so
#: keeps binding: an unparseable record is never permission to land. The
#: watchdog's `landable_seams` reads the same line the same way.
_SEAT_REVIEWED_KEY = re.compile(r"^\s*reviewed\s*:", re.IGNORECASE)
_SEAT_REVIEWED_SHA = re.compile(r"^\s*reviewed\s*:\s*([0-9a-fA-F]{7,64})\s*$", re.IGNORECASE)

#: Bytes read from one seat file. A review is a few short lines. Past this a
#: first line is not a verdict a person wrote, and reading it whole is how a
#: multi-megabyte line stalls `build` under its lock.
_SEAT_READ_BOUND = 8192
#: Characters kept of the cause. The page shows one line, not the file.
_SEAT_CAUSE_CAP = 200


def _read_seat(seat: Path) -> tuple[float, str, str, str]:
    """(mtime, verdict word, reviewed sha or "", one-line cause or "") of one seat file.

    Only a regular file is opened, and only the first `_SEAT_READ_BOUND`
    bytes, so a FIFO, a symlink, or a device never reaches `readline`. A
    bare verdict (no cause on that line) takes the first later non-blank
    line that is not the `Reviewed:` line. The cause is at most
    `_SEAT_CAUSE_CAP` characters.
    """
    info = seat.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise OSError("seat is not a regular file")
    when = info.st_mtime
    with seat.open("rb") as fh:
        blob = fh.read(_SEAT_READ_BOUND)
    if len(blob) == _SEAT_READ_BOUND and b"\n" in blob and not blob.endswith(b"\n"):
        blob = blob.rsplit(b"\n", 1)[0]
    lines = blob.decode("utf-8", errors="replace").splitlines()
    first = lines[0].strip() if lines else ""
    verdict, _, cause = first.partition(" ")
    verdict = verdict.upper()
    cause = cause.strip()
    reviewed = ""
    for line in lines[1:]:
        if _SEAT_REVIEWED_KEY.match(line):
            found = _SEAT_REVIEWED_SHA.match(line)
            reviewed = found.group(1).lower() if found else ""
            break
    if not cause:
        for line in lines[1:]:
            if not line.strip() or _SEAT_REVIEWED_KEY.match(line):
                continue
            cause = line.strip()
            break
    return when, verdict, reviewed, cause[:_SEAT_CAUSE_CAP]


def _review_predates_tip(sha: str, row: str, repo: Path | None, remote: str | None,
                         runner: Runner) -> str:
    """Why a verdict on `sha` no longer speaks for the row's pushed tip, else "".

    A seam rebased and re-cited to answer a HOLD has a new tip, and the commit
    the reviewer judged is no longer on it. Only a positive answer from git
    supersedes: no repository, a tip that does not resolve, or a git that could
    not run all leave the verdict binding.
    """
    if repo is None or not remote:
        return ""
    rc, out = runner(["git", "rev-parse", "--verify", "--quiet",
                      f"refs/remotes/{remote}/seam/{row}^{{commit}}"], cwd=repo)
    tip = out.strip()
    if rc != 0 or not tip:
        return ""
    rc, _ = runner(["git", "merge-base", "--is-ancestor", sha, tip], cwd=repo)
    if rc is None or rc == 0:
        return ""
    return f"review predates the current tip: reviewed {sha[:12]} is not an ancestor of {tip[:12]}"


#: The verdict `_review_readings` gives a seat file it could not read, when the
#: caller asked for unreadable seats to be kept. Never a word a seat file holds.
_UNREADABLE_SEAT = "UNREADABLE"


def _review_readings(reviews: Path | None, repo: Path | None, remote: str | None,
                     runner: Runner | None, keep_unreadable: bool
                     ) -> dict[str, tuple[str, str, str]] | None:
    """Row id -> (verdict, why superseded or "", one-line cause) from the newest seat per row.

    The one reading of the review seats: `review_standings` drops the cause and
    skips an unreadable seat (so an older readable one may speak for the row),
    `apply_review_reasons` keeps the cause and, with `keep_unreadable`, lets an
    unreadable newest seat stand as `_UNREADABLE_SEAT` so the page can say
    "unknown". None when the directory cannot be listed at all — `iterdir`,
    not `glob`, because `glob` swallows a permission error and looks like no
    seats. A path that is not a regular file (a FIFO, a symlink, a device)
    is skipped: opening it can hang the page under its lock.
    """
    if reviews is None or not reviews.is_dir():
        return {}
    run = runner if runner is not None else sv.run_cmd
    latest: dict[str, tuple[float, str, str, str]] = {}
    try:
        seats = sorted(
            path for path in reviews.iterdir()
            if path.name.endswith(".seat") and not path.name.startswith(".")
        )
    except OSError:
        return None
    for seat in seats:
        row = seat.name.split(".", 1)[0]
        try:
            if not stat.S_ISREG(seat.lstat().st_mode):
                continue
            when, verdict, reviewed, cause = _read_seat(seat)
        except OSError:
            if not keep_unreadable:
                continue
            try:
                when = seat.stat().st_mtime
            except OSError:
                when = float("inf")
            verdict, reviewed, cause = _UNREADABLE_SEAT, "", ""
        seen = latest.get(row)
        if seen is None or when >= seen[0]:
            latest[row] = (when, verdict, reviewed, cause)
    readings: dict[str, tuple[str, str, str]] = {}
    for row, (_when, verdict, reviewed, cause) in latest.items():
        why = _review_predates_tip(reviewed, row, repo, remote, run) if reviewed else ""
        readings[row] = ("SUPERSEDED", why, cause) if why else (verdict, "", cause)
    return readings


def review_standings(reviews: Path | None, repo: Path | None = None, remote: str | None = None,
                     runner: Runner | None = None) -> dict[str, tuple[str, str]]:
    """Row id -> (verdict, why it was superseded or "") from the newest seat file per row.

    Read from `<reviews>/<Id>.<batch>.seat`, whose first line is the verdict
    (`.claude/skills/autonomous-build/SKILL.md`) and which may carry a
    `Reviewed: <sha>` line naming the tip it judged. The newest file per row
    wins, since a later batch's review supersedes an earlier one. A verdict
    whose sha is not an ancestor of the row's pushed tip is superseded: its
    verdict reads "SUPERSEDED" and the reason says the review predates the
    tip. A verdict naming no sha stays as written.
    """
    readings = _review_readings(reviews, repo, remote, runner, keep_unreadable=False)
    return {row: (verdict, why) for row, (verdict, why, _cause) in (readings or {}).items()}


def held_seams(reviews: Path | None, repo: Path | None = None, remote: str | None = None,
               runner: Runner | None = None) -> set[str]:
    """Row ids whose latest, still-current review says HOLD or NEEDS-OWNER.

    An unreadable or absent directory yields an empty set, which counts every
    seam as outstanding — the cautious direction, because a banner that fires
    early tells someone to switch off a box that is still wanted. See
    `review_standings` for which verdict is the latest and when it is superseded.
    """
    standings = review_standings(reviews, repo, remote, runner)
    return {row for row, (verdict, _why) in standings.items() if verdict in HELD_VERDICTS}


def apply_review_reasons(seams: list, reviews: Path | None, repo: Path | None = None,
                         remote: str | None = None, runner: Runner | None = None) -> None:
    """Apply each row's newest still-current review to its seam, hiding no unreadable seat.

    Reads the seats the way `review_standings` does (newest per row, and a
    review that predates the pushed tip is SUPERSEDED), and in addition keeps
    the verdict's one-line cause. A HOLD or NEEDS-OWNER shows `VERDICT: cause`;
    a seat that cannot be read, or whose verdict is no word the seats use, shows
    "unknown"; a LAND leaves the seam's own reason; a SUPERSEDED review is no
    hold, so it leaves the seam's reason alone and is not shown as one.
    """
    if reviews is None or not reviews.is_dir():
        return
    readings = _review_readings(reviews, repo, remote, runner, keep_unreadable=True)
    if readings is None:
        for seam in seams:
            if not seam.merged:
                seam.reason, seam.review_verdict = "unknown", "UNKNOWN"
        return
    for seam in seams:
        if seam.merged or seam.row_id not in readings:
            continue
        verdict, _why, cause = readings[seam.row_id]
        if verdict in ("LAND", "SUPERSEDED"):
            seam.review_verdict = verdict
        elif verdict in HELD_VERDICTS:
            seam.review_verdict = verdict
            seam.reason = f"{verdict}: {cause or 'unknown'}"
        else:
            seam.review_verdict, seam.reason = "UNKNOWN", "unknown"
