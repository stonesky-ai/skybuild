"""Seam status: GET /seams joined to the items' priority and finish date.

Maps /seams to a flat table of every seam in the current trunk target, sorted by priority
and date, with counts of seam states. Data from the todo service's /seams endpoint plus
priority and finish date from the items table.
"""
from __future__ import annotations

import re
from collections.abc import Mapping

_DEV_TARGET = re.compile(r"^dev-\d+_features$")


def build_seam_status(seams_doc: Mapping, items: Mapping[str, Mapping]) -> dict:
    """Build the seam status section from the seams doc and items.

    Args:
        seams_doc: The /seams response dict with seams list and rev.
        items: Dict mapping seam id to item dict with priority, state, updated_at.

    Returns:
        A dict with trunk, rows (list of dicts), counts (dict of state counts),
        and total (int). A note field is set if items data was unavailable.
    """
    # Find the greatest dev-N_features target
    seams_list = seams_doc.get("seams", [])
    dev_targets = [s["target"] for s in seams_list
                   if isinstance(s, dict) and isinstance(s.get("target"), str) and _DEV_TARGET.match(s["target"])]
    trunk = max(dev_targets) if dev_targets else ""

    # Keep only rows for the trunk target
    kept_seams = [s for s in seams_list
                  if isinstance(s, dict) and s.get("target") == trunk]

    rows = []
    counts: dict[str, int] = {}

    for seam in kept_seams:
        if not isinstance(seam, dict):
            continue

        seam_id = seam.get("id")
        if not isinstance(seam_id, str):
            continue

        state = seam.get("state")
        if not isinstance(state, str):
            state = ""

        # Count this state
        counts[state] = counts.get(state, 0) + 1

        # Get item data for priority and finish date
        item = items.get(seam_id, {})
        if not isinstance(item, dict):
            item = {}

        priority = item.get("priority")
        priority_val = priority if isinstance(priority, int) else ""

        # Finish date: only set if item state is pushed or landed
        item_state = item.get("state")
        finished = ""
        if item_state in ("pushed", "landed"):
            updated_at = item.get("updated_at")
            if isinstance(updated_at, str):
                # Format: first 16 chars (YYYY-MM-DD HH:MM with T replaced by space)
                finished = updated_at[:16].replace("T", " ")

        # Since time
        since = seam.get("since")
        since_str = ""
        if isinstance(since, str):
            since_str = since[:16].replace("T", " ")

        why = seam.get("why")
        if not isinstance(why, str):
            why = ""

        # Build row with exact key order as specified
        # seam field: remove leading "seam/" from branch name if present
        seam_field = seam.get("seam", seam_id)
        if isinstance(seam_field, str) and seam_field.startswith("seam/"):
            seam_field = seam_field[5:]

        row = {
            "seam": seam_field,
            "state": state,
            "next": seam.get("next", ""),
            "priority": priority_val,
            "finished": finished,
            "since": since_str,
            "why": why,
        }

        # Keep only string/int values
        if all(isinstance(row.get(k), (str, int, type(None))) for k in row):
            rows.append(row)

    # Sort by priority ascending (empty string sorts last), then by since ascending
    def sort_key(row: dict) -> tuple:
        p = row.get("priority")
        p_sort = (1, 0) if p == "" else (0, p)
        s = row.get("since", "")
        return (p_sort[0], p_sort[1], s)

    rows.sort(key=sort_key)

    result = {
        "trunk": trunk,
        "rows": rows,
        "counts": counts,
        "total": len(rows),
    }

    return result
