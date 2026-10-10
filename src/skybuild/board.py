"""Read-only workflow board, without execution or mutation authority."""

from datetime import datetime, timezone

from .completion import current_completion
from .workflow import Place


def _entered_at(task):
    value = task.get("metadata", {}).get("_skybuild_workflow", {}).get("petri", {}).get("place_entered_at")
    try:
        result = datetime.fromisoformat(value)
        return result if result.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def board_snapshot(tasks, project_id, complete_dependencies, projection, *, limit=100, offset=0, now=None):
    """Aggregate every row while retaining only one bounded page of cards."""
    now = now or datetime.now(timezone.utc)
    columns = {place.value: {"place": place.value, "count": 0, "oldest_age_seconds": None,
                            "unknown_age_count": 0} for place in Place}
    cards, total, unenrolled = [], 0, 0
    dependencies_complete = dependencies_blocked = 0
    for task in tasks:
        public = projection(task)
        place = public.get("place")
        if place not in columns:
            unenrolled += 1
            continue
        column = columns[place]
        column["count"] += 1
        entered = _entered_at(task)
        if entered is None:
            column["unknown_age_count"] += 1
        else:
            age = max(0, int((now - entered).total_seconds()))
            column["oldest_age_seconds"] = max(column["oldest_age_seconds"] or 0, age)
        blocked = [dependency for dependency in task["dependencies"] if dependency not in complete_dependencies]
        if place == Place.READY.value:
            dependencies_complete += not blocked
            dependencies_blocked += bool(blocked)
        if offset <= total < offset + limit:
            token = task.get("metadata", {}).get("_skybuild_workflow", {}).get("petri", {}).get("token", {})
            cards.append({**{key: task.get(key) for key in (
                "task_id", "title", "priority", "revision", "next_action", "blocker", "responsible", "dependencies")},
                          **public, **{key: token.get(key) for key in ("hold_reason", "deferred_until", "milestone_task_id")},
                          "place_entered_at": entered.isoformat() if entered else None,
                          "blocked_dependencies": blocked})
        total += 1
    return {"project_id": project_id, "columns": list(columns.values()), "tasks": cards,
            "total": total, "unenrolled_count": unenrolled, "offset": offset,
            "next_offset": offset + limit if total > offset + limit else None,
            "ready_dependencies_complete": dependencies_complete,
            "ready_dependencies_blocked": dependencies_blocked}


class BoardQueries:
    """Use the existing Store authorization and connection boundaries."""

    def workflow_board(self, principal, project_id, *, limit=100, offset=0):
        from .store import TASK_SELECT, _public

        self._page(limit, offset)
        # Configure the snapshot before Store identity and authorization reads.
        with self._connection(consistent_snapshot=True) as connection:
            self._authorize(connection, principal, project_id, "tasks:read")
            complete = set()
            dependency_select = TASK_SELECT.replace(
                "SELECT *,", "SELECT *, (SELECT input_generation = assessed_generation "
                "FROM task_readiness r WHERE r.project_id = tasks.project_id "
                "AND r.task_id = tasks.task_id) AS dependency_current,", 1)
            dependencies = connection.execute(
                dependency_select + "WHERE project_id = %s AND EXISTS (SELECT 1 FROM task_dependencies d "
                "WHERE d.project_id = tasks.project_id AND d.dependency_id = tasks.task_id)", (project_id,))
            for dependency in dependencies:
                if current_completion(dependency) and dependency["dependency_current"] is True:
                    complete.add(dependency["task_id"])
            rows = connection.execute(
                TASK_SELECT + "WHERE project_id = %s ORDER BY priority, "
                "metadata #>> '{_skybuild_workflow,petri,place_entered_at}' NULLS LAST, task_id", (project_id,))
            return _public(board_snapshot(rows, project_id, complete, self.workflow_projection,
                                          limit=limit, offset=offset))
