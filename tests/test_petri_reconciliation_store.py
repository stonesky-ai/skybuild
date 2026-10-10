"""Disposable PostgreSQL acceptance cases for Petri reconciliation."""
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from skybuild.contracts import DomainError
from skybuild.store import Store
from skybuild.workflow import Place
from test_store import actors, create, store
from test_petri_store import enrolled
from test_petri_acceptance_store import completed_petri_fixture


def test_edit_preserves_hold_and_matches_actual_generation(store, actors):
    project, people = actors
    owner = people["owner"]
    task = create(store, owner, project, acceptance_criteria=["Check"])
    task = store.initialize_workflow(owner, project, task["task_id"], task["revision"], "initialize")["task"]
    changed = store.update_task(owner, project, task["task_id"], {"description": "Changed scope"}, task["revision"], "edit")
    token = Store.workflow_token(changed)
    assert token.place == Place.HOLD and token.hold_reason
    assert token.input_generation == changed["metadata"]["_skybuild_workflow"]["readiness"]["input_generation"]
    assert token.definition_revision == changed["revision"]
    released = store.workflow_transition(owner, project, task["task_id"], "release_hold",
                                         {"reason": "Definition reviewed"}, changed["revision"], "release")["task"]
    assert Store.workflow_token(released).place == Place.READY
    assert released["metadata"]["_skybuild_workflow"]["readiness"]["assessed_generation"] == token.input_generation


def test_deferred_edit_preserves_trigger_and_due_resume_is_cpu_only(store, actors):
    project, people = actors
    owner = people["owner"]
    task = create(store, owner, project)
    task = store.task_action(owner, project, task["task_id"], "defer",
                             {"reason": "Later", "until": "2090-01-01T00:00:00+00:00"}, task["revision"], "defer")
    task = store.initialize_workflow(owner, project, task["task_id"], task["revision"], "initialize")["task"]
    assert Store.workflow_token(task).deferred_until == "2090-01-01T00:00:00+00:00"
    changed = store.update_task(owner, project, task["task_id"], {"description": "Changed scope"}, task["revision"], "edit")
    assert Store.workflow_token(changed).place == Place.DEFERRED
    result = store.reconcile_due_deferrals(owner, project, "sweep", now=datetime(2091, 1, 1, tzinfo=timezone.utc))
    assert result["reassessed"] == [task["task_id"]]
    resumed = store.get_task(owner, project, task["task_id"])
    assert Store.workflow_token(resumed).place == Place.READY
    assert Store.workflow_token(resumed).attempt_id is None
    assert store.claim_history(owner, project, task["task_id"]) == []


def test_milestone_done_label_without_current_completion_cannot_resume(store, actors):
    project, people = actors
    owner = people["owner"]
    create(store, owner, project, "milestone")
    task = create(store, owner, project, "waiting")
    task = store.task_action(owner, project, "waiting", "defer", {"reason": "Wait", "milestone_task_id": "milestone"},
                             task["revision"], "defer")
    task = store.initialize_workflow(owner, project, "waiting", task["revision"], "initialize")["task"]
    with store._connection() as connection:
        connection.execute("UPDATE tasks SET status = 'done' WHERE project_id = %s AND task_id = 'milestone'", (project,))
    assert store.reconcile_due_deferrals(owner, project, "sweep")["reassessed"] == []
    assert store.get_task(owner, project, "waiting") == task


def test_ready_can_show_unmet_dependency_but_claim_cannot_start(store, actors):
    project, people = actors
    owner = people["owner"]
    create(store, owner, project, "unmet")
    task = create(store, owner, project, "waiting", dependencies=["unmet"], acceptance_criteria=["Check"])
    task = store.initialize_workflow(owner, project, "waiting", task["revision"], "initialize")["task"]
    task = store.task_action(owner, project, "waiting", "ready", {"reason": "Definition checked"}, task["revision"], "ready")
    assert Store.workflow_token(task).place == Place.READY
    with pytest.raises(DomainError, match="lacks current completion"):
        store.claim_task(people["worker"], project, "waiting", task["revision"], "claim")


def test_resolved_started_scope_split_preserves_history_and_retires_to_hold(store, actors):
    project, people = actors
    owner = people["owner"]
    task = enrolled(store, people, project)
    claim = store.claim_task(people["worker"], project, task["task_id"], task["revision"], "claim")
    store.release_claim(people["worker"], project, task["task_id"], claim["fence"], claim["task_revision"], "release", reason="Checkpoint complete")
    task = store.get_task(owner, project, task["task_id"])
    old_history = store.task_history(owner, project, task["task_id"])
    children = [{"task_id": task_id, "title": task_id, "description": "Replacement scope",
                 "acceptance_criteria": task["acceptance_criteria"], "architecture_refs": task["architecture_refs"],
                 "dependencies": task["dependencies"]} for task_id in ("child-a", "child-b")]
    result = store.split_task(owner, project, task["task_id"], children, {}, "Split resolved work", task["revision"], "split")
    token = Store.workflow_token(result["source"])
    assert token.place == Place.HOLD and token.superseded
    assert result["source"]["metadata"]["_skybuild_workflow"]["replaced_by"] == ["child-a", "child-b"]
    assert store.task_history(owner, project, task["task_id"])[:len(old_history)] == old_history
    assert all(Store.workflow_token(child).place == Place.READY for child in result["children"])
    with pytest.raises(DomainError):
        store.workflow_transition(owner, project, task["task_id"], "release_hold", {"reason": "Invalid resurrection"},
                                  result["source"]["revision"], "release-retired")


def test_reopen_invalidates_dependency_and_keeps_accepted_history(store, actors):
    from skybuild.completion import current_completion
    project, people = actors
    owner = people["owner"]
    task = completed_petri_fixture(store, people, project, "accepted")
    create(store, owner, project, "dependent", dependencies=["accepted"])
    old_generation = Store.workflow_token(task).input_generation
    changed = store.workflow_transition(owner, project, "accepted", "reopen", {"reason": "Acceptance changed"},
                                        task["revision"], "reopen")["task"]
    assert Store.workflow_token(changed).place == Place.READY
    assert Store.workflow_token(changed).input_generation == old_generation + 1
    assert not current_completion(changed)
    assert any(current_completion(event["after_state"]) for event in store.task_history(owner, project, "accepted"))
    assert store.get_task(owner, project, "dependent")["phase"] == "reassess"


def test_ready_resume_does_not_admit_incomplete_definition(store, actors):
    project, people = actors
    owner = people["owner"]
    task = create(store, owner, project)
    task = store.initialize_workflow(owner, project, task["task_id"], task["revision"], "initialize")["task"]
    with pytest.raises(DomainError):
        store.workflow_transition(owner, project, task["task_id"], "release_hold", {"reason": "Reassess definition"},
                                  task["revision"], "release")
    assert Store.workflow_token(store.get_task(owner, project, task["task_id"])).place == Place.HOLD
    with pytest.raises(DomainError):
        store.claim_task(people["worker"], project, task["task_id"], task["revision"], "claim")


def test_reassess_current_done_does_not_destroy_acceptance(store, actors):
    from skybuild.completion import current_completion
    project, people = actors
    owner = people["owner"]
    task = completed_petri_fixture(store, people, project, "accepted")
    generation = Store.workflow_token(task).input_generation
    changed = store.task_action(owner, project, "accepted", "reassess", {"reason": "Acceptance remains current"},
                                task["revision"], "reassess")
    assert Store.workflow_token(changed).place == Place.DONE
    assert Store.workflow_token(changed).input_generation == generation
    assert current_completion(changed)


@pytest.mark.parametrize("event", ["hold", "defer"])
def test_http_control_with_held_claim_stays_pending(store, actors, event):
    from fastapi.testclient import TestClient
    from skybuild.api import create_app
    project, people = actors
    task = enrolled(store, people, project)
    store.claim_task(people["worker"], project, task["task_id"], task["revision"], "claim")
    task = store.get_task(people["owner"], project, task["task_id"])
    request = {"event": event, "reason": "Owner interruption"}
    if event == "defer":
        request["until"] = "2090-01-01T00:00:00+00:00"
    path = f"/api/v1/projects/{project}/tasks/{task['task_id']}/workflow"
    with TestClient(create_app(store)) as client:
        response = client.post(path, json=request, headers={"Authorization": "Bearer " + people["owner_token"],
                               "If-Match": str(task["revision"]), "Idempotency-Key": "owner-control"})
        assert response.status_code == 200
        token = response.json()["token"]
        assert token["place"] == "working" and token["pending_action"] == event
        assert client.get(path, headers={"Authorization": "Bearer " + people["owner_token"]}).json()["token"] == token
    with store._connection() as connection:
        assert connection.execute("SELECT held FROM task_claims WHERE project_id = %s AND task_id = %s",
                                  (project, task["task_id"])).fetchone()["held"] is True


def test_live_claim_submission_and_result_preserve_pending_hold(store, actors):
    from skybuild.workflow import ResultState, ValidationResult, ValidationStage
    from test_petri_store import author_receipt
    project, people = actors
    task = enrolled(store, people, project)
    store.claim_task(people["worker"], project, task["task_id"], task["revision"], "claim")
    task = store.get_task(people["owner"], project, task["task_id"])
    token = Store.workflow_token(task)
    submitted = store.workflow_transition(people["worker"], project, task["task_id"], "submit", author_receipt(token),
                                          token.revision, "submit")
    assert submitted["token"]["place"] == "validating"
    held = store.workflow_transition(people["owner"], project, task["task_id"], "hold", {"reason": "Pause after check"},
                                     submitted["task"]["revision"], "hold")
    token = Store.workflow_token(held["task"])
    assert token.place == Place.VALIDATING and token.pending_action == "hold"
    result = ValidationResult(project, task["task_id"], ValidationStage.UNIT_TESTS, ResultState.PASSED,
        attempt_id=token.attempt_id, source_head=token.source_head, target_base=token.target_base,
        input_generation=token.input_generation, definition_revision=token.definition_revision,
        policy_version=token.policy_version, claim_fence=token.claim_fence, producer=people["worker"].principal_id, check_id="unit")
    recorded = store.workflow_transition(people["worker"], project, task["task_id"], "validation_result", {"result": result.to_dict()},
                                         token.revision, "result")
    assert recorded["token"]["pending_action"] == "hold"
    assert recorded["token"]["place"] == "validating"
    assert recorded["token"]["evidence"][0]["state"] == "passed"


def test_current_done_reassessment_does_not_invalidate_or_block_dependents(store, actors):
    project, people = actors
    owner = people["owner"]
    prerequisite = completed_petri_fixture(store, people, project, "accepted-a")
    completed_petri_fixture(store, people, project, "accepted-b", dependencies=["accepted-a"])
    dependent = create(store, owner, project, "ready-c", dependencies=["accepted-a"], acceptance_criteria=["Check"])
    dependent = store.task_action(owner, project, "ready-c", "ready", {"reason": "Ready"}, dependent["revision"], "ready")
    dependent = store.initialize_workflow(owner, project, "ready-c", dependent["revision"], "initialize-c")["task"]
    store.claim_task(people["worker"], project, "ready-c", dependent["revision"], "claim-c")
    before = {task_id: store.get_task(owner, project, task_id) for task_id in ("accepted-b", "ready-c")}
    store.task_action(owner, project, "accepted-a", "reassess", {"reason": "Acceptance unchanged"},
                      prerequisite["revision"], "reassess-a")
    assert {task_id: store.get_task(owner, project, task_id) for task_id in before} == before
