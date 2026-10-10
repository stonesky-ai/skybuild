"""PostgreSQL board scope, ordering, and complete project totals."""

import pytest

from skybuild.contracts import DomainError
from test_store import store, actors, create


def test_board_complete_project_and_priority_order(store, actors):
    project, people = actors
    for index in range(105):
        task = create(store, people["owner"], project, f"BOARD-{index:03}", priority=1 if index == 104 else 2)
        store.initialize_workflow(people["owner"], project, task["task_id"], task["revision"], f"init-{index}")
    create(store, people["owner"], project, "legacy")
    first = store.workflow_board(people["worker"], project, limit=100)
    assert first["total"] == 105
    assert first["unenrolled_count"] == 1
    assert first["tasks"][0]["task_id"] == "BOARD-104"
    assert first["next_offset"] == 100
    assert next(column for column in first["columns"] if column["place"] == "hold")["count"] == 105
    last = store.workflow_board(people["worker"], project, limit=100, offset=100)
    assert len(last["tasks"]) == 5 and last["next_offset"] is None
    assert last["total"] == first["total"]
    assert [(column["place"], column["count"]) for column in last["columns"]] == [(column["place"], column["count"]) for column in first["columns"]]
    with pytest.raises(DomainError) as caught:
        store.workflow_board(people["outsider"], project)
    assert caught.value.status_code == 403
    other = store.workflow_board(people["owner"], project + "-other")
    assert other["total"] == 0 and other["unenrolled_count"] == 0


def test_bare_done_dependency_does_not_show_current_acceptance(store, actors):
    from test_petri_store import enrolled
    project, people = actors
    dependency = create(store, people["owner"], project, "dependency")
    task = enrolled(store, people, project, "ready")
    with store._connection() as connection:
        connection.execute("UPDATE tasks SET status = 'done' WHERE project_id = %s AND task_id = %s", (project, dependency["task_id"]))
        connection.execute("INSERT INTO task_dependencies VALUES (%s, %s, %s)", (project, task["task_id"], dependency["task_id"]))
    result = store.workflow_board(people["worker"], project)
    assert result["ready_dependencies_complete"] == 0
    assert result["ready_dependencies_blocked"] == 1
    assert result["tasks"][0]["blocked_dependencies"] == ["dependency"]
