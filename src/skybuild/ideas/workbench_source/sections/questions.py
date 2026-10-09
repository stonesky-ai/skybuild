"""UserQuestions: what the owner must answer to uncork waiting work, from Unresolved.md and the loops.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .. import hub as sv

#: `Unresolved N` on a brief's `depends on:` line: that row waits on the owner's answer.
_WAITS_ON_UNRESOLVED = re.compile(r"\bUnresolved\s+#?(\d+)\b")
#: The stamp line the loops' own `notify` writes under a NEEDS-OWNER.md title.
_NOTE_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
#: At most this many lines of one NEEDS-OWNER.md body go on the page; the rest are counted.
NEEDS_OWNER_LINES = 20


def _stated_default(text: str) -> tuple[str, str]:
    """(question, default): the default is the parenthesis closing the line, nested ones and all.

    Unresolved.md's own rule is "the parenthesis is what the build assumes while
    you are silent", and a default may itself hold parentheses ("assumed (a) —
    …"), so the match is found by depth from the end. No closing parenthesis, or
    an unbalanced one, is a line that states no default: "".
    """
    if not text.endswith(")"):
        return text, ""
    depth = 0
    for at in range(len(text) - 1, -1, -1):
        if text[at] == ")":
            depth += 1
        elif text[at] == "(":
            depth -= 1
            if depth == 0:
                return text[:at].rstrip(), text[at + 1:-1].strip()
    return text, ""


def read_open_questions(path: Path) -> tuple[str, list[dict]]:
    """("", each open `N. question (default)` line) or (why unreadable, []).

    The same numbered lines `read_unresolved` counts. An indented line directly
    under one (a later correction, say) belongs to it and is only counted here:
    the page points at the file for it rather than repeat a paragraph.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"cannot read {path.name}: {exc.strerror or exc}", []
    found: list[dict] = []
    current: dict | None = None
    for line in text.splitlines():
        if m := sv._QUESTION.match(line):
            question, default = _stated_default(sv._shown(m.group(2)))
            current = {"number": int(m.group(1)), "question": question, "default": default, "more": 0}
            found.append(current)
        elif current is not None and line.strip() and line[:1].isspace():
            current["more"] += 1
        else:
            current = None
    return "", found


def unresolved_waiters(briefs: Path) -> tuple[str, dict[int, list[str]]]:
    """("", Unresolved number -> the rows whose brief `depends on:` it) or (why unreadable, {}).

    Only a `depends on:` line — found with `claim.py`'s own pattern — makes a row
    wait: `claim.py` refuses to start a row while its dependencies are open, and
    a `refs:` or `do:` mention of a line already answered holds nothing up.
    """
    if not briefs.is_dir():
        return f"cannot read {briefs.name}/: no such directory", {}
    waiting: dict[int, list[str]] = {}
    for brief in sorted(briefs.glob("*.md")):
        try:
            lines = brief.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        row = sv._shown(brief.stem)
        for m in filter(None, map(sv.claims.claim.DEPENDS.match, lines)):
            for number in _WAITS_ON_UNRESOLVED.findall(m["rest"]):
                rows = waiting.setdefault(int(number), [])
                if row not in rows:
                    rows.append(row)
    return "", waiting


def _held_row(loop, stopped: bool) -> str:
    """The row a loop's note holds up, or "".

    A park notice names its row (`parked.json`'s `notice_row`, written with the
    note); a loop the note stopped holds the row its stream's state file was on.
    A running loop's other notes hold no row: it has gone on to the next one.
    """
    try:
        parks = json.loads((loop.state_dir / "parked.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        parks = None
    row = parks.get("notice_row") if isinstance(parks, dict) else None
    if isinstance(row, str) and row.strip():
        return sv._shown(row)
    if stopped and loop.checkout is not None and loop.stream:
        held = sv.read_state_file(loop.checkout / ".agent-state" / f"{loop.stream}.md").get("row", "")
        if not sv._is_none(held):
            return sv._shown(held)
    return ""


def _loop_question(loop, now: float) -> dict | None:
    """The loop's NEEDS-OWNER.md as a question, or None when it has none or a later start answered it.

    Whether the note is live is `sv._needs_owner`'s rule (a note older than the
    loop's last start is history: restarting is how the owner answers), so the
    terminal's NEEDS OWNER line and this box cannot disagree. The whole note is
    shown, title first, less the stamp line the loop writes under it.
    """
    path = loop.state_dir / "NEEDS-OWNER.md"
    if sv._needs_owner(path, loop) is None:
        return None
    try:
        written = path.stat().st_mtime
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = [shown for line in text.splitlines()
             if (shown := sv._shown(line)) and not _NOTE_STAMP.match(shown)]
    title = re.sub(r"^#+\s*", "", lines[0]) if lines else "NEEDS-OWNER.md"
    body = lines[1:]
    if len(body) > NEEDS_OWNER_LINES:
        body = body[:NEEDS_OWNER_LINES] + [f"(+{len(body) - NEEDS_OWNER_LINES} more lines in {path.name})"]
    stream = sv._shown(loop.stream or loop.state_dir.name)
    stopped = loop.phase == "stopped"
    row = _held_row(loop, stopped)
    blocks = f"loop {stream}" + (" (stopped)" if stopped else "") + (f" · row {row}" if row else "")
    return {"from": f"{stream} {path.name}", "question": "\n".join([title, *body]),
            "default": ("none — the loop stays stopped until it is restarted" if stopped
                        else "the loop goes on with its other work meanwhile"),
            "blocks": blocks, "asked": sv.age(now - written)}


def userquestions_section(unresolved: Path, briefs: Path, loops, now: float) -> dict:
    """What the owner must answer to uncork waiting work, each with what it blocks.

    First every named loop's live NEEDS-OWNER.md (a process waits on it now),
    then every open Unresolved.md line with its stated default and the rows
    whose brief waits on it. The loops are `sv.read_loop_steps`' — the
    directories SESSIONVIEW_STREAM_STATE names, the Sub-agents box's own list —
    so with none named that half refuses by the setting's name (ADR-0002) while
    the Unresolved lines still show. Everything here is read, and every string
    is the text as written: the page puts it on screen as text, never markup.
    Nothing open is an empty list, never an error.
    """
    problems: list[str] = []
    error, open_lines = read_open_questions(unresolved)
    if error:
        problems.append(error)
    if loops.refused:
        problems.append(loops.refused)
    questions = [q for loop in loops.loops if (q := _loop_question(loop, now)) is not None]
    brief_error, waiting = unresolved_waiters(briefs) if open_lines else ("", {})
    if brief_error:
        problems.append(brief_error)
    for item in open_lines:
        more = item["more"]
        rows = waiting.get(item["number"], [])
        if brief_error:
            blocks = f"unknown ({brief_error})"
        elif rows:
            blocks = ("row " if len(rows) == 1 else "rows ") + ", ".join(rows)
        else:
            blocks = "no filed row waits on it"
        questions.append({
            "from": f"Unresolved {item['number']}",
            "question": item["question"] + (f" (+{more} more line{'s' if more != 1 else ''} in "
                                            f"{unresolved.name})" if more else ""),
            "default": item["default"],
            "blocks": blocks,
            "asked": "",
        })
    return {"error": "; ".join(problems), "questions": questions}
