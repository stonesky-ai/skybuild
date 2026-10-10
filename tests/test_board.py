"""Read-only board summaries remain complete across bounded card pages."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from skybuild.board import board_snapshot
from skybuild.api import create_app
from skybuild.contracts import Principal
from skybuild.workflow import Place


def task(index, place="ready", dependencies=()):
    return {"task_id": f"T{index:03}", "title": "<script>unsafe</script>", "priority": 2,
            "revision": 1, "dependencies": list(dependencies), "responsible": "owner",
            "metadata": {"_skybuild_workflow": {"petri": {"token": {"place": place},
                "place_entered_at": "2026-01-01T00:00:00+00:00"}}}}


def project(row):
    return {"place": row["metadata"]["_skybuild_workflow"]["petri"]["token"]["place"],
            "validation": [], "enabled_actions": [], "evidence_freshness": "current"}


def test_complete_totals_and_dependency_counts_are_independent_of_page():
    rows = [task(index, dependencies=("accepted",) if index % 2 else ("unfinished",)) for index in range(205)]
    rows.append(task(999, None))
    result = board_snapshot(iter(rows), "project", {"accepted"}, project, limit=100, offset=100,
                            now=datetime(2026, 1, 2, tzinfo=timezone.utc))
    assert len(result["tasks"]) == 100
    assert result["total"] == 205 and result["unenrolled_count"] == 1
    assert result["next_offset"] == 200
    assert result["ready_dependencies_complete"] == 102
    assert result["ready_dependencies_blocked"] == 103
    assert result["columns"][0]["count"] == 205
    assert result["columns"][0]["oldest_age_seconds"] == 86400
    assert result["tasks"][0]["blocked_dependencies"] == ["unfinished"]
    assert result["tasks"][0]["title"] == "<script>unsafe</script>"
    assert [column["place"] for column in result["columns"]] == [place.value for place in Place]


@pytest.mark.parametrize("timestamp", [None, "bad", "2026-01-01T00:00:00"])
def test_missing_or_invalid_age_is_unknown(timestamp):
    row = task(1)
    row["metadata"]["_skybuild_workflow"]["petri"]["place_entered_at"] = timestamp
    result = board_snapshot([row], "project", set(), project)
    assert result["columns"][0]["oldest_age_seconds"] is None
    assert result["columns"][0]["unknown_age_count"] == 1


def test_board_route_forwards_authenticated_scope_and_bounds():
    class Store:
        def authenticate(self, token):
            return Principal("worker", False, {"project": frozenset({"tasks:read"})})
        def workflow_board(self, actor, project_id, **kwargs):
            assert actor.principal_id == "worker"
            return {"project_id": project_id, **kwargs}
    with TestClient(create_app(Store())) as client:
        assert client.get("/api/v1/projects/project/workflow-board").status_code == 401
        headers = {"Authorization": "Bearer test"}
        result = client.get("/api/v1/projects/project/workflow-board?limit=5&offset=10", headers=headers)
        assert result.json() == {"project_id": "project", "limit": 5, "offset": 10}
        for query in ("limit=0", "limit=101", "offset=-1"):
            assert client.get("/api/v1/projects/project/workflow-board?" + query, headers=headers).status_code == 422
