"""Separate user metadata limits from bounded, internally managed evidence."""
from copy import deepcopy
from uuid import uuid4

import pytest

from skybuild.contracts import DomainError
from skybuild.store import Store, _json, _validate_metadata
from skybuild.workflow import TaskToken, Place, ValidationResult, ValidationStage, ResultState
from test_store import store, actors, create


def managed_metadata():
    result = ValidationResult("project", "task", ValidationStage.UNIT_TESTS, ResultState.FAILED,
                              findings=tuple("Fault " + str(n) + ": " + "x" * 1000 for n in range(6)))
    token = TaskToken("project", "task", Place.READY, evidence=(result,), faults=("Correct unit tests",))
    return {"legacy": "x" * (10 * 1024),
            "_skybuild_workflow": {"generation": 1, "petri": {"schema_version": 1, "token": token.to_dict()}}}


def test_valid_user_data_and_failed_result_have_separate_budgets():
    metadata = managed_metadata()
    assert 16 * 1024 < len(_json(metadata).encode()) < 64 * 1024
    assert _validate_metadata(metadata) is metadata
    values = Store._task_values({"title": "Task", "description": "Brief", "metadata": metadata})
    assert values["metadata"] == metadata


def test_ordinary_edit_inherits_valid_managed_evidence():
    values = Store._task_values({"title": "Task", "description": "Brief", "metadata": managed_metadata()})
    updated = Store._task_values({"title": "Updated task"}, before=values)
    assert updated["metadata"] == values["metadata"]
    assert updated["title"] == "Updated task"


def test_user_metadata_limit_remains_16_kib_with_or_without_managed_records():
    for metadata in ({"legacy": "x" * (16 * 1024)}, managed_metadata()):
        metadata["legacy"] = "x" * (16 * 1024)
        with pytest.raises(DomainError, match="User metadata"):
            _validate_metadata(metadata)


def test_managed_aggregate_and_nested_record_limits_remain_bounded():
    metadata = managed_metadata()
    metadata["_skybuild_completion"] = {"evidence": "x" * (64 * 1024)}
    with pytest.raises(DomainError, match="64 KiB"):
        _validate_metadata(metadata)
    metadata = managed_metadata()
    metadata["_skybuild_workflow"]["petri"]["token"]["links"] = ["x" * 4000] * 5
    with pytest.raises(DomainError, match="16 KiB"):
        _validate_metadata(metadata)


@pytest.mark.parametrize("key", ["_skybuild_workflow", "_skybuild_completion"])
def test_reserved_shapes_remain_objects(key):
    with pytest.raises(DomainError, match="objects"):
        _validate_metadata({key: "invalid"})


def test_versioned_token_shape_is_checked():
    for petri in ({}, {"schema_version": True, "token": {}}, {"schema_version": 1, "token": []}):
        with pytest.raises(DomainError):
            _validate_metadata({"_skybuild_workflow": {"petri": petri}})


def test_public_create_rejects_oversized_and_reserved_metadata(store, actors):
    project, people = actors
    for metadata in ({"legacy": "x" * (16 * 1024)},
                     {"_skybuild_workflow": {}}, {"_skybuild_completion": {}}):
        with pytest.raises(DomainError):
            create(store, people["owner"], project, "invalid-" + uuid4().hex, metadata=metadata)


def test_confirmed_failure_with_10_kib_legacy_data_commits_ready(store, actors):
    from skybuild.workflow import TRANSITIONS
    if not any(spec.event == "validation_result" for spec in TRANSITIONS):
        pytest.skip("Run the composed Task03/04 candidate")
    project, people = actors
    task = create(store, people["owner"], project, "large-user-failure",
                  metadata={"legacy": "x" * (10 * 1024)}, acceptance_criteria=["Repair failed checks"])
    task = store.task_action(people["owner"], project, task["task_id"], "ready", {"reason": "Reviewed"}, task["revision"], "ready")
    task = store.initialize_workflow(people["owner"], project, task["task_id"], task["revision"], "initialize")["task"]
    claim = store.claim_task(people["worker"], project, task["task_id"], task["revision"], "claim")
    task = store.get_task(people["owner"], project, task["task_id"])
    token = Store.workflow_token(task)
    receipt = {"source_head": "a" * 40, "target_base": "b" * 40, "source_branch": "refs/heads/task/output",
               "attempt_id": token.attempt_id, "claim_fence": token.claim_fence, "input_generation": token.input_generation,
               "definition_revision": token.definition_revision, "policy_version": token.policy_version}
    task = store.workflow_transition(people["worker"], project, task["task_id"], "submit", receipt, task["revision"], "submit")["task"]
    token = Store.workflow_token(task)
    result = ValidationResult(project, task["task_id"], ValidationStage.UNIT_TESTS, ResultState.FAILED,
        attempt_id=token.attempt_id, source_head=token.source_head, target_base=token.target_base,
        input_generation=token.input_generation, definition_revision=token.definition_revision,
        policy_version=token.policy_version, claim_fence=token.claim_fence, producer=people["worker"].principal_id,
        findings=tuple("Fault " + str(n) + ": " + "x" * 1000 for n in range(6)), check_id="unit")
    view = store.workflow_transition(people["worker"], project, task["task_id"], "validation_result", {"result": result.to_dict()},
                                     task["revision"], "failure")
    assert view["token"]["place"] == "ready"
    assert view["task"]["metadata"]["legacy"] == "x" * (10 * 1024)
    assert len(_json(view["task"]["metadata"]).encode()) > 16 * 1024
    with store._connection() as connection:
        event = connection.execute("SELECT event_facts FROM task_journal WHERE project_id = %s AND task_id = %s AND operation = 'workflow.validation_result'",
                                   (project, task["task_id"])).fetchone()
    assert event["event_facts"]["result"] == result.to_dict()


def test_public_edit_rejects_reserved_or_oversized_user_fields(store, actors):
    project, people = actors
    task = create(store, people["owner"], project)
    for metadata in ({"legacy": "x" * (16 * 1024)}, {"_skybuild_workflow": {}}, {"_skybuild_completion": {}}):
        with pytest.raises(DomainError):
            store.update_task(people["owner"], project, task["task_id"], {"metadata": metadata}, task["revision"], uuid4().hex)
