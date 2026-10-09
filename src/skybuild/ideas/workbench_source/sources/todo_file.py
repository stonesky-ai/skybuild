"""The to-do file: MasterToDo.md's counts by state.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------- to-do file

STATE_BUCKETS = [
    ("in process", ("🟡",)),
    ("blocked", ("⏸", "🔴")),
    ("done", ("✅", "✓")),
    ("queued", ("☐", "⬜", "[ ]")),
]


@dataclass
class TodoCounts:
    total: int = 0
    buckets: dict[str, int] = field(default_factory=dict)
    ids_in_process: list[str] = field(default_factory=list)
    ids: list[str] = field(default_factory=list)
    error: str | None = None


def read_todo(path: Path) -> TodoCounts:
    counts = TodoCounts()
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        counts.error = f"cannot read {path}: {exc.strerror}"
        return counts

    in_table = False
    state_col = -1
    id_col = 0
    for line in lines:
        if not line.lstrip().startswith("|"):
            in_table = False
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        lowered = [c.lower() for c in cells]
        if any(re.fullmatch(r"-{2,}|:?-+:?", c) for c in cells):
            continue                                   # separator row
        if "state" in lowered or "status" in lowered:  # header row: arm the table
            in_table = True
            state_col = lowered.index("state") if "state" in lowered else lowered.index("status")
            id_col = 0
            continue
        if lowered[:1] == ["id"] and "do" in lowered:  # `| Id | P | Do |`: no state column
            in_table, state_col, id_col = True, -1, 0
            continue
        if not in_table or state_col >= len(cells):
            continue
        counts.total += 1
        counts.ids.append(re.sub(r"[*`]", "", cells[id_col]))
        if state_col < 0:
            # A row is open until its landing commit deletes it; whether anyone
            # holds it is the seam branches' business, not the file's.
            counts.buckets["open"] = counts.buckets.get("open", 0) + 1
            continue
        state = cells[state_col]
        for name, glyphs in STATE_BUCKETS:
            if any(g in state for g in glyphs):
                counts.buckets[name] = counts.buckets.get(name, 0) + 1
                if name == "in process":
                    counts.ids_in_process.append(re.sub(r"[*`]", "", cells[id_col]))
                break
        else:
            counts.buckets["other"] = counts.buckets.get("other", 0) + 1
    return counts
