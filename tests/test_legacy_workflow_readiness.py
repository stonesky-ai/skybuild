"""Imported legacy tasks can lack bookkeeping created by normal task writes."""
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from skybuild.api import create_app
from skybuild.contracts import DomainError
from test_claims import ready
from test_effects import intent
from test_store import actors, create, store


def legacy_without_marker(store, people, project, *, status="proposed"):
    task_id = "legacy-" + uuid4().hex
    task = (ready(store, people, project, task_id) if status == "ready" else
            create(store, people["owner"], project, task_id,
                   acceptance_criteria=["Preserve imported history"]))
    # Frozen imports insert their journal directly after migration 005's one-time
    # backfill. Remove only synthetic fixture bookkeeping to reproduce that state.
    with store._connection() as connection:
        connection.execute("DELETE FROM task_readiness WHERE project_id = %s AND task_id = %s",
                           (project, task["task_id"]))
    return task


def marker(store, project, task_id):
    with store._connection() as connection:
        return connection.execute("SELECT * FROM task_readiness WHERE project_id = %s AND task_id = %s",
                                  (project, task_id)).fetchone()


def test_http_initialization_repairs_missing_marker_atomically_and_replays(store, actors):
    project, people = actors
    task = legacy_without_marker(store, people, project)
    history = store.task_history(people["owner"], project, task["task_id"])
    path = f"/api/v1/projects/{project}/tasks/{task['task_id']}/workflow"
    headers = {"Authorization": "Bearer " + people["owner_token"],
               "If-Match": str(task["revision"]), "Idempotency-Key": "legacy-initialize"}
    # Use the actual HTTP error response. Before the repair this request returns
    # sanitized 503 when enrollment subscripts a missing readiness row.
    with TestClient(create_app(store), raise_server_exceptions=False) as client:
        response = client.post(path, json={"event": "initialize"}, headers=headers)
        assert response.status_code == 200, response.text
        initialized = response.json()
        assert client.post(path, json={"event": "initialize"}, headers=headers).json() == initialized
    assert initialized["token"]["place"] == "hold"
    assert initialized["token"]["input_generation"] == 1
    assert initialized["token"]["attempt_id"] is None
    assert initialized["task"]["revision"] == task["revision"] + 1
    assert initialized["task"]["status"] == task["status"]
    assert marker(store, project, task["task_id"]) == {
        "project_id": project, "task_id": task["task_id"],
        "input_generation": 1, "assessed_generation": 0,
    }
    after = store.task_history(people["owner"], project, task["task_id"])
    assert after[:-1] == history
    assert after[-1]["operation"] == "workflow_initialized"
    assert after[-1]["before_state"] == task


def test_missing_marker_does_not_assess_ready_task_or_authorize_claim(store, actors):
    project, people = actors
    task = legacy_without_marker(store, people, project, status="ready")
    initialized = store.initialize_workflow(people["owner"], project, task["task_id"],
                                            task["revision"], "ready-initialize")
    assert initialized["token"]["place"] == "ready"
    assert initialized["token"]["evidence"] == []
    assert "claim" not in initialized["available_actions"]
    assert marker(store, project, task["task_id"])["assessed_generation"] == 0
    before_claim = store.get_task(people["owner"], project, task["task_id"])
    with pytest.raises(DomainError) as caught:
        store.claim_task(people["worker"], project, task["task_id"],
                         initialized["task"]["revision"], "unassessed-claim")
    assert caught.value.code == "workflow_conflict"
    assert store.claim_history(people["owner"], project, task["task_id"]) == []
    assert store.get_task(people["owner"], project, task["task_id"]) == before_claim


@pytest.mark.parametrize("assessed", [0, 5, 7])
def test_initialization_preserves_existing_readiness_generations(store, actors, assessed):
    project, people = actors
    task = ready(store, people, project, "existing-" + uuid4().hex)
    with store._connection() as connection:
        connection.execute("UPDATE task_readiness SET input_generation = 7, assessed_generation = %s "
                           "WHERE project_id = %s AND task_id = %s", (assessed, project, task["task_id"]))
    before = marker(store, project, task["task_id"])
    initialized = store.initialize_workflow(people["owner"], project, task["task_id"],
                                            task["revision"], "existing-initialize")
    assert initialized["token"]["input_generation"] == 7
    assert marker(store, project, task["task_id"]) == before
    assert ("claim" in initialized["available_actions"]) == (assessed == 7)


@pytest.mark.parametrize("guard,code", [("worker", "authorization"), ("stale", "stale_revision"),
                                      ("effect", "effect_conflict"), ("claim", "claim_conflict")])
def test_enrollment_guards_do_not_create_missing_marker(store, actors, guard, code):
    project, people = actors
    task = legacy_without_marker(store, people, project, status="ready")
    if guard == "effect":
        intent(store, people["owner"], project, task)
    elif guard == "claim":
        store.claim_task(people["worker"], project, task["task_id"], task["revision"], "legacy-held-claim")
    history = store.task_history(people["owner"], project, task["task_id"])
    principal = people["worker" if guard == "worker" else "owner"]
    revision = task["revision"] + (guard == "stale")
    with pytest.raises(DomainError) as caught:
        store.initialize_workflow(principal, project, task["task_id"], revision, "guarded-initialize")
    assert caught.value.code == code
    assert marker(store, project, task["task_id"]) is None
    assert store.get_task(people["owner"], project, task["task_id"]) == task
    assert store.task_history(people["owner"], project, task["task_id"]) == history


def test_failed_enrollment_journal_rolls_back_new_marker_and_idempotency(store, actors, monkeypatch):
    project, people = actors
    task = legacy_without_marker(store, people, project)
    history = store.task_history(people["owner"], project, task["task_id"])
    original = store._journal

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("Injected enrollment journal failure")

    with monkeypatch.context() as patch:
        patch.setattr(store, "_journal", fail)
        with pytest.raises(RuntimeError, match="Injected enrollment journal failure"):
            store.initialize_workflow(people["owner"], project, task["task_id"],
                                      task["revision"], "rollback-initialize")
    assert marker(store, project, task["task_id"]) is None
    assert store.get_task(people["owner"], project, task["task_id"]) == task
    assert store.task_history(people["owner"], project, task["task_id"]) == history
    with store._connection() as connection:
        assert connection.execute("SELECT 1 FROM idempotency WHERE principal_id = %s AND project_id = %s "
                                  "AND operation = 'workflow.initialize' AND idempotency_key = 'rollback-initialize'",
                                  (people["owner"].principal_id, project)).fetchone() is None
    retried = store.initialize_workflow(people["owner"], project, task["task_id"],
                                       task["revision"], "rollback-initialize")
    assert retried["task"]["revision"] == task["revision"] + 1
    assert marker(store, project, task["task_id"]) is not None
