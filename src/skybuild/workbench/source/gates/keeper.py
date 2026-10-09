"""Why each keeper's sessions end idle: the keeper journal over `SESSIONVIEW_WEB_KEEPER_WINDOW_MINUTES`.
"""
from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Sequence

from . import hub as sv

# ---------------------------------------------------------------- why each keeper's sessions end idle

#: The session keeper's unit (`scripts/agents/units/wowbagger-session-keeper/`). Its journal says how each
#: session ended and, on every tick whose reason changed, why the keeper holds the next one back.
KEEPER_UNIT = "skykeep-session-keeper.service"
#: How far back the keeper journal is read, in whole minutes. No default (ADR-0002): unset reads nothing.
KEEPER_WINDOW_VAR = "SESSIONVIEW_WEB_KEEPER_WINDOW_MINUTES"
#: A row whose sessions ended idle this often in the window is spinning: the keeper restarts it and the
#: session finds nothing to do (a finished seam, a row it cannot start), every back-off, for hours.
KEEPER_SPIN_IDLE_ENDS = 3
#: A session that ended sooner than this did no work worth its start.
KEEPER_SHORT_MINUTES = 1.0

JournalReader = Callable[[Sequence[str]], "str | None"]

_KEEPER_LINE = re.compile(r"^\d{4}-\d\d-\d\d (\d\d:\d\d:\d\d) (.*)$")
_KEEPER_STARTED = re.compile(r"^slot (\d+) started: .*?\brow ([A-Z0-9][A-Z0-9-]*) attempt \d+")
_KEEPER_ENDED = re.compile(r"^slot (\d+) ended: .*?\bstatus=(\S+).*?\bruntime=(\S+)")
#: Keeper lines that are events, not a tick's reason, though some quote one (`slot 2 stopped: <reason>`).
_KEEPER_EVENT = re.compile(r"^(?:slot \d+\b|NEEDS-OWNER |settings |keeper start|stop latch|stop-all|row |"
                           r"the brief of |no session started|STOP appeared|weekly usage known again)")
#: A tick's first clause when it reports what runs (`runs 1 of 3`, `adaptive: runs 0, target 1 ...`).
_KEEPER_HEAD = re.compile(r"^(?:adaptive: )?runs \d+")
#: A tick's clauses that hold a start back, in `session_keeper.decide`'s words; the first match names a clause.
_HOLD_BACK = (
    ("tripwire", re.compile(r"^tripwire: ")),
    ("memory funds 0", re.compile(r"\bmemory funds 0 more\b")),
    ("stale usage reading", re.compile(r"\breading is \d+ min old\b|^weekly usage unknown\b")),
    ("gate lock", re.compile(r"^(?:a gate runs, or its lock\b|gate lock: )")),
    ("stack row on a slot with no lane", re.compile(r"^slot \d+ has no lane, and \S+ needs a stack\b")),
    ("stack row on a gate's lane", re.compile(r"^slot \d+ has lane \d+, which a running gate shares\b")),
    ("back-off", re.compile(r"^slot\(s\) [\d, ]+ in back-off\b")),
    ("held for the owner", re.compile(r"\bheld for the owner\b")),
    ("outside a bound", re.compile(r"^outside a bound\b")),
    ("no start", re.compile(r"\b(?:no (?:\w+ )?session starts|nothing starts|every \w+ stopped)\b")),
)


def keeper_window() -> tuple[int | None, str]:
    """(minutes, "") from `SESSIONVIEW_WEB_KEEPER_WINDOW_MINUTES`, else (None, what is wrong with it)."""
    raw = os.environ.get(KEEPER_WINDOW_VAR, "").strip()
    if not raw:
        return None, f"window unset: {KEEPER_WINDOW_VAR} names none, so no keeper journal is read"
    if not re.fullmatch(r"[0-9]+", raw) or int(raw) <= 0:
        return None, f"window invalid: {KEEPER_WINDOW_VAR}={raw[:24]!r} is not a positive whole number of minutes"
    return int(raw), ""


def read_journal(argv: Sequence[str]) -> str | None:
    """`journalctl`'s output, or None when it could not be read: failed, missing or hung is unknown, never empty."""
    try:
        done = subprocess.run(list(argv), capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 and done.stdout is not None else None


def _clauses(text: str) -> list[str]:
    """A tick's `; `-joined clauses; a `; ` inside parentheses stays in its clause."""
    out, depth, start = [], 0, 0
    for i, ch in enumerate(text):
        depth += (ch == "(") - (ch == ")" and depth > 0)
        if depth == 0 and text.startswith("; ", i):
            out.append(text[start:i])
            start = i + 2
    out.append(text[start:])
    return [c.strip() for c in out if c.strip()]


def _hold_back_label(clause: str) -> str | None:
    return next((label for label, pattern in _HOLD_BACK if pattern.search(clause)), None)


def summarise_keeper_journal(text: str, minutes: int) -> dict:
    """Sessions ended in the window, how many idle or short, the rows spinning idle, the newest tick's hold-backs.

    An "ended" line names no row: the row is the one its slot's last "started" line named, so an end whose
    start is before the window counts in the totals and names no row.
    """
    slot_row: dict[str, str] = {}
    idle_by_row: dict[str, int] = {}
    lines = ended = idle = short = 0
    tick: tuple[str, list[tuple[str, str]], str] | None = None
    for raw in text.splitlines():
        m = _KEEPER_LINE.match(raw)
        if not m:
            continue                                   # systemd's own lines carry no keeper timestamp
        lines += 1
        clock, msg = m.groups()
        if started := _KEEPER_STARTED.match(msg):
            slot_row[started.group(1)] = started.group(2)
        elif end := _KEEPER_ENDED.match(msg):
            ended += 1
            try:
                short += float(end.group(3)) < KEEPER_SHORT_MINUTES
            except ValueError:
                pass                                   # runtime=? is not short, and not long either
            if end.group(2) == "idle":
                idle += 1
                if row := slot_row.get(end.group(1)):
                    idle_by_row[row] = idle_by_row.get(row, 0) + 1
        elif not _KEEPER_EVENT.match(msg):
            clauses = _clauses(msg)
            held = [(label, c) for c in clauses if (label := _hold_back_label(c))]
            if held or (clauses and _KEEPER_HEAD.match(clauses[0])):
                tick = (clock, held, msg)
    spinning = sorted(((r, n) for r, n in idle_by_row.items() if n >= KEEPER_SPIN_IDLE_ENDS), key=lambda x: (-x[1], x[0]))
    return {"state": "read", "window_minutes": minutes, "lines": lines, "ended": ended, "idle": idle, "short": short,
            "spinning": [{"row": r, "idle_ends": n} for r, n in spinning],
            "tick_at": tick[0] if tick else None,
            "hold_back": [{"why": label, "clause": sv._cmdline(c, 160)} for label, c in tick[1]] if tick else [],
            "tick": sv._cmdline(tick[2], 200) if tick else ""}


def keeper_idle(journal: JournalReader = read_journal) -> dict:
    """This box's keeper over the configured window, from its journal (read only); unset reads nothing."""
    minutes, why = keeper_window()
    if minutes is None:
        return {"state": why.split(":", 1)[0], "text": why}
    text = journal(["journalctl", "--user", "-u", KEEPER_UNIT, "--since", f"-{minutes}min", "-o", "cat",
                    "--no-pager", "-q"])
    if text is None:
        return {"state": "unknown", "window_minutes": minutes, "text": f"unknown: the {KEEPER_UNIT} journal could not be read"}
    return summarise_keeper_journal(text, minutes)


def _keeper_window_source() -> str:
    """Python a box runs ahead of `remote_probe()`: this box's window, as a literal, so both read one setting."""
    raw = os.environ.get(KEEPER_WINDOW_VAR)
    set_it = f"_o.environ[{KEEPER_WINDOW_VAR!r}] = {raw!r}" if raw is not None else f"_o.environ.pop({KEEPER_WINDOW_VAR!r}, None)"
    return f"\nimport os as _o\n{set_it}\n"


def keeper_columns(keeper: dict | None, why_none: str = "the box's probe sent no keeper summary") -> dict:
    """The fleet row's two keeper cells: how its sessions ended lately, and what the newest tick holds back."""
    if keeper is None:
        return {"keeper_idle": f"unknown: {why_none}", "keeper_hold_back": "unknown"}
    if keeper.get("state") != "read":
        text = str(keeper.get("text") or "unknown")
        return {"keeper_idle": text, "keeper_hold_back": text.split(":", 1)[0]}
    mins = int(keeper["window_minutes"])
    if not keeper.get("lines"):
        idle = f"no keeper journal line in the last {mins} min"
    else:
        idle = (f"{int(keeper['ended'])} ended in the last {mins} min: {int(keeper['idle'])} idle, "
                f"{int(keeper['short'])} under {KEEPER_SHORT_MINUTES:g} min; ")
        spins = [f"{s['row']} ({int(s['idle_ends'])} idle ends)" for s in keeper.get("spinning") or []]
        idle += (f"spinning idle ({KEEPER_SPIN_IDLE_ENDS}+ idle ends): " + ", ".join(spins)) if spins else "no row spins idle"
    if not keeper.get("tick_at"):
        hold = f"no tick line in the last {mins} min (the keeper logs its reason only when it changes)"
    elif keeper.get("hold_back"):
        hold = f"at {keeper['tick_at']}: " + " | ".join(f"{h['why']}: {h['clause']}" for h in keeper["hold_back"])
    else:
        hold = f"at {keeper['tick_at']}: none named ({keeper.get('tick', '')})"
    return {"keeper_idle": idle, "keeper_hold_back": hold}
