"""Explicit, finite CPU timer for catch-up through the authenticated task API.

No timer runs on import or API startup. Restart begins a fresh sweep; guarded
resume transitions and stable task IDs make repeated pages safe.
"""

import time
from collections.abc import Callable

from .client import Client, ClientError
from .contracts import valid_identifier


def schedule_due(client: Client, project_id: str, *, interval_seconds: int = 60,
                 max_ticks: int = 60, page_size: int = 100, max_pages: int = 20,
                 report: Callable[[dict], None] = lambda result: None,
                 sleep: Callable[[float], None] = time.sleep) -> dict:
    """Run bounded sweeps, carrying incomplete pages into the next timer tick.

    First tick catches up immediately. Delay starts after each completed tick,
    so slow API calls cannot create a burst of missed historical timer ticks.
    Any unconfirmed page stops the timer and retains its retry cursor in output.
    """
    bounds = ((interval_seconds, 1, 86400), (max_ticks, 1, 10000),
              (page_size, 1, 100), (max_pages, 1, 100))
    if any(type(value) is not int or not lower <= value <= upper for value, lower, upper in bounds):
        raise ValueError("Scheduler bounds are invalid")
    if not valid_identifier(project_id):
        raise ValueError("Project ID is not addressable")
    cursor = None
    seen_cursors = set()
    for tick in range(1, max_ticks + 1):
        scanned, reassessed = 0, []
        try:
            for _ in range(max_pages):
                page = client.reconcile_due_deferrals(project_id, limit=page_size, after_task_id=cursor)
                count, changed, next_cursor = page["scanned"], page["reassessed"], page["next_after_task_id"]
                if (type(count) is not int or not 0 <= count <= page_size or
                        not isinstance(changed, list) or len(changed) > count or
                        any(not valid_identifier(task_id) for task_id in changed) or
                        (next_cursor is not None and (not valid_identifier(next_cursor) or
                         count != page_size or next_cursor in seen_cursors))):
                    raise ValueError("Invalid reconciliation response")
                scanned += count
                reassessed.extend(changed)
                cursor = next_cursor
                if cursor is not None:
                    seen_cursors.add(cursor)
                else:
                    seen_cursors.clear()
                if cursor is None:
                    break
        except (ClientError, ValueError, KeyError, TypeError):
            result = {"tick": tick, "scanned": scanned, "reassessed": reassessed,
                      "complete": False, "next_after_task_id": cursor, "uncertain_page": True}
            report(result)
            return result
        result = {"tick": tick, "scanned": scanned, "reassessed": reassessed,
                  "complete": cursor is None, "next_after_task_id": cursor, "uncertain_page": False}
        report(result)
        if tick < max_ticks:
            sleep(interval_seconds)
    return result
