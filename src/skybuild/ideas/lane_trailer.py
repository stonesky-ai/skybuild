"""The tip's `Lane:` trailer, read and judged in one place.

`seam_merge`, `mergeprep`, `factory_doctor` and `claim` each read the same
trailer; this leaf module holds the pattern, the git format string, the split
and the pure verdict they share. It imports no sibling, so any of them can load
it without a cycle. The checks that need git (is the sha a commit, is it on the
branch) stay with each caller.
"""

from __future__ import annotations

import re

#: The `Lane:` trailer's value (AGENTS.md § Parallel development, rule 4). The exit
#: takes a sign so a signal's negative code reads as a failed lane, not a typo.
LANE = re.compile(
    r"testfast (?P<passed>\d+) passed, (?P<failed>\d+) failed, "
    r"exit (?P<exit>-?\d+) at (?P<sha>[0-9a-f]{7,64})"
)
LANE_FORMAT = "Lane: testfast <passed> passed, <failed> failed, exit 0 at <sha>"
#: The `git log -1 --format=` string that prints every `Lane:` trailer, unit-separated.
TRAILER_FORMAT = "%(trailers:key=Lane,valueonly=true,unfold=true,separator=%x1f)"
#: How many characters of a malformed value a detail sentence quotes.
SAID_CHARS = 120


def value(passed: int, sha: str) -> str:
    """A green `Lane:` value, for the one script that writes the trailer rather than reads it."""
    return f"testfast {int(passed)} passed, 0 failed, exit 0 at {sha}"


def values(raw: str) -> list[str]:
    """The non-empty `Lane:` values in the output of `git log -1 --format=TRAILER_FORMAT`."""
    return [v.strip() for v in raw.split("\x1f") if v.strip()]


def _said(value: str) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= SAID_CHARS else text[: SAID_CHARS - 1] + "…"


def verdict(found: list[str]) -> tuple[str | None, str, re.Match | None]:
    """(problem, detail, match) for the tip's trailer values; no git is asked.

    The problem is `missing`, `several`, `malformed`, `red`, `empty` or None. A
    value of the lane shape is the match, whatever its problem; None means the
    trailer is green and only the sha is left to check.
    """
    if not found:
        return "missing", "the tip carries no `Lane:` trailer", None
    if len(found) > 1:
        return "several", f"the tip carries {len(found)} `Lane:` trailers", None
    lane = LANE.fullmatch(found[0])
    if not lane:
        return "malformed", f"`Lane: {_said(found[0])}` is not `{LANE_FORMAT}`", None
    if int(lane["exit"]) != 0 or int(lane["failed"]) != 0:
        return "red", f"{lane['failed']} failed, exit {lane['exit']}", lane
    if int(lane["passed"]) == 0:
        return "empty", "the lane records no passing test", lane
    return None, "", lane
