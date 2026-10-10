"""Record bounds, task isolation and legacy workflow compatibility."""

from dataclasses import FrozenInstanceError, replace
import json

import pytest

from skybuild.contracts import DomainError
from skybuild.workflow import (
    Place, ResultState, TaskToken, TransitionSpec, ValidationResult,
    ValidationStage, action_change,
)


def result(**changes):
    values = dict(project_id="project", task_id="TASK-1", stage=ValidationStage.UNIT_TESTS,
                  state=ResultState.PASSED, source_head="abc", target_base="def",
                  attempt_id="attempt-1", input_generation=2, definition_revision=3,
                  policy_version="policy-1", producer="worker-1", check_id="pytest",
                  parameters=(("command", "pytest -q"),), tool_version="8.0",
                  artifacts=("artifact:unit-log",))
    values.update(changes)
    return ValidationResult(**values)


def test_full_token_json_round_trip_and_no_mutable_aliases():
    token = TaskToken("project", "TASK-1", place=Place.VALIDATING,
                      requirements=tuple(ValidationStage), evidence=(result(),),
                      dependencies=("TASK-0",), links=("https://example.org/pr/1",))
    body = json.loads(json.dumps(token.to_dict()))
    assert TaskToken.from_dict(body) == token
    body["evidence"][0]["artifacts"].append("other")
    body["dependencies"].append("TASK-2")
    assert token.evidence[0].artifacts == ("artifact:unit-log",)
    assert token.dependencies == ("TASK-0",)
    with pytest.raises(FrozenInstanceError):
        token.place = Place.READY
    with pytest.raises(FrozenInstanceError):
        token.evidence[0].state = ResultState.FAILED


def test_spec_serialization_supports_current_place_destination():
    for destination in (None, Place.WORKING):
        spec = TransitionSpec("claim", (Place.READY,), destination, "admission")
        assert TransitionSpec.from_dict(json.loads(json.dumps(spec.to_dict()))) == spec


@pytest.mark.parametrize("record, field, value", [
    (TaskToken, "place", "ready"), (TaskToken, "priority", True),
    (TaskToken, "input_generation", -1), (TaskToken, "revision", 2**31),
    (TaskToken, "claim_fence", False), (TaskToken, "title", "x" * 501),
    (TaskToken, "responsible", "x" * 201), (TaskToken, "next_action", "x" * 4097),
    (TaskToken, "dependencies", ["TASK-0"]), (TaskToken, "dependencies", ("bad/id",)),
    (TaskToken, "requirements", (ValidationStage.SCANS,) * 2),
    (TaskToken, "superseded", 1), (TaskToken, "evidence", [result()]),
    (ValidationResult, "stage", "unit_tests"), (ValidationResult, "state", "passed"),
    (ValidationResult, "artifacts", ["log"]),
    (ValidationResult, "parameters", (("name", "x"), ("name", "y"))),
    (ValidationResult, "parameters", (["name", "x"],)),
    (ValidationResult, "definition_revision", -1),
    (ValidationResult, "policy_version", "x" * 201),
])
def test_direct_records_reject_wrong_types_and_bounds(record, field, value):
    with pytest.raises(DomainError) as error:
        if record is TaskToken:
            record("project", "TASK-1", **{field: value})
        else:
            result(**{field: value})
    assert error.value.status_code == 422


@pytest.mark.parametrize("changes", [
    {"project_id": "other"}, {"task_id": "TASK-2"},
])
def test_token_rejects_evidence_from_other_task(changes):
    with pytest.raises(DomainError):
        TaskToken("project", "TASK-1", evidence=(result(**changes),))


@pytest.mark.parametrize("record,body", [
    (TaskToken, {"project_id": "project", "task_id": "TASK-1", "unknown": 1}),
    (TaskToken, {"project_id": "project", "task_id": "TASK-1", "place": "R"}),
    (TaskToken, {"project_id": "project", "task_id": "TASK-1", "evidence": {}}),
    (TaskToken, {"project_id": "project", "task_id": "TASK-1", "requirements": ["unknown"]}),
    (TaskToken, {"project_id": "project", "task_id": "TASK-1", "links": [float("nan")]}),
    (TaskToken, {"project_id": "project", "task_id": "TASK-1", "priority": None}),
    (ValidationResult, {"project_id": "project", "task_id": "TASK-1", "stage": "scans"}),
    (TransitionSpec, {"event": "claim", "sources": ["ready"], "destination": "working"}),
])
def test_json_records_reject_unknown_incomplete_or_invalid_input(record, body):
    with pytest.raises(DomainError):
        record.from_dict(body)


def test_record_size_is_utf8_bounded_and_arrays_are_bounded():
    with pytest.raises(DomainError, match="16 KiB"):
        TaskToken("project", "TASK-1", links=("é" * 4000,) * 3)
    with pytest.raises(DomainError):
        TaskToken("project", "TASK-1", links=("x",) * 101)
    with pytest.raises(DomainError):
        ValidationResult.from_dict({**result().to_dict(), "findings": ["x" * 20_000]})


def test_invalid_deep_json_and_non_json_values_raise_domain_error():
    body = {"project_id": "project", "task_id": "TASK-1"}
    body["links"] = body
    with pytest.raises(DomainError):
        TaskToken.from_dict(body)
    with pytest.raises(DomainError):
        TaskToken.from_dict({"project_id": object()})


def test_exact_seven_places_and_five_stages():
    assert {item.value for item in Place} == {
        "ready", "working", "validating", "integrating", "done", "deferred", "hold"}
    assert len(ValidationStage) == 5
    assert replace(result(), state=ResultState.STALE).state is ResultState.STALE


def test_manual_ready_behavior_remains_unchanged_and_does_not_grant_claim():
    before = dict(task_id="TASK-1", status="proposed", phase="triage",
                  responsible="owner", metadata={})
    change = action_change(before, "ready", {"reason": "Definition is complete"})
    assert change["status"] == "ready"
    assert change["phase"] == "ready-for-work"
    assert change["next_action"] == "Await explicit admission and ownership"
    assert before["metadata"] == {}
    assert change["metadata"]["_skybuild_workflow"]["generation"] == 1
