"""Material changes preserve owner controls and invalidate exact token evidence."""
from copy import deepcopy
from dataclasses import replace

import pytest

from skybuild.reconciliation import replacement_token, synchronize_token
from skybuild.store import Store
from skybuild.workflow import Place, ResultState, TaskToken, ValidationResult, ValidationStage


def state(place):
    evidence = ValidationResult("project", "task", ValidationStage.UNIT_TESTS, ResultState.PASSED,
                                input_generation=4, definition_revision=3)
    token = TaskToken("project", "task", place, input_generation=4, definition_revision=3,
                      revision=5, evidence=(evidence,), hold_reason="Owner choice" if place in {Place.HOLD, Place.DEFERRED} else None,
                      deferred_until="2035-01-01T00:00:00+00:00" if place == Place.DEFERRED else None)
    status = "deferred" if place == Place.DEFERRED else "blocked" if place == Place.HOLD else "in-progress"
    before = {"project_id": "project", "task_id": "task", "revision": 5, "title": "Task", "description": "Scope",
              "priority": 2, "dependencies": ["prerequisite"], "acceptance_criteria": ["Check"],
              "architecture_refs": [], "assignee": None, "status": status, "phase": place.value,
              "responsible": "owner", "next_action": "Keep owner choice", "blocker": "Owner choice",
              "metadata": {"_skybuild_workflow": {"generation": 2, "readiness": {"input_generation": 4, "assessed_generation": 4},
                           "deferral": {"until": "2035-01-01T00:00:00+00:00"},
                           "petri": {"schema_version": 1, "token": token.to_dict(), "place_entered_at": "2025-01-01T00:00:00+00:00"}}}}
    values = Store._task_values({}, before)
    values["metadata"] = deepcopy(before["metadata"])
    values["metadata"]["_skybuild_workflow"]["readiness"]["input_generation"] = 5
    values.update(status="blocked", phase="reassess", blocker="Changed", next_action="Reassess changed definition")
    return before, values


@pytest.mark.parametrize("place", list(Place))
def test_material_invalidation_stales_evidence_without_implicit_movement(place):
    before, values = state(place)
    synchronize_token(before, values, operation="updated", reason="Definition changed")
    token = TaskToken.from_dict(values["metadata"]["_skybuild_workflow"]["petri"]["token"])
    assert token.place == place
    assert token.input_generation == 5 and token.definition_revision == 6
    assert token.evidence[0].state == ResultState.STALE
    assert values["metadata"]["_skybuild_workflow"]["petri"]["place_entered_at"] == "2025-01-01T00:00:00+00:00"
    assert before["metadata"]["_skybuild_workflow"]["petri"]["token"]["evidence"][0]["state"] == "passed"


@pytest.mark.parametrize("place", [Place.HOLD, Place.DEFERRED])
def test_dependency_invalidation_preserves_explicit_owner_control(place):
    before, values = state(place)
    values["metadata"]["_skybuild_workflow"].pop("deferral")
    synchronize_token(before, values, operation="dependency_invalidated", reason="Prerequisite reopened")
    token = TaskToken.from_dict(values["metadata"]["_skybuild_workflow"]["petri"]["token"])
    assert token.place == place and token.hold_reason == "Owner choice"
    assert values["blocker"] == "Owner choice" and values["next_action"] == "Keep owner choice"
    assert values["metadata"]["_skybuild_workflow"]["deferral"] == before["metadata"]["_skybuild_workflow"]["deferral"]


@pytest.mark.parametrize("operation", ["split", "merge"])
def test_scope_retirement_is_hold_superseded_with_stale_evidence(operation):
    before, values = state(Place.VALIDATING)
    values["metadata"]["_skybuild_workflow"]["replaced_by"] = ["replacement"]
    synchronize_token(before, values, operation=operation, reason="Scope replaced")
    token = TaskToken.from_dict(values["metadata"]["_skybuild_workflow"]["petri"]["token"])
    assert token.place == Place.HOLD and token.superseded
    assert token.evidence[0].state == ResultState.STALE
    assert values["status"] == "superseded"
    assert values["metadata"]["_skybuild_workflow"]["replaced_by"] == ["replacement"]


def test_replacement_has_new_identity_and_no_prior_attempt_or_evidence():
    before, values = state(Place.READY)
    replacement_token("project", "replacement", values)
    token = TaskToken.from_dict(values["metadata"]["_skybuild_workflow"]["petri"]["token"])
    assert token.task_id == "replacement" and token.place == Place.READY
    assert token.evidence == () and token.attempt_id is None and token.claim_fence is None
    assert token.dependencies == ("prerequisite",) and token.priority == 2


def test_legacy_task_does_not_get_an_implicit_token():
    before, values = state(Place.READY)
    values["metadata"]["_skybuild_workflow"].pop("petri")
    synchronize_token(before, values, operation="updated", reason="Edit legacy task")
    assert "petri" not in values["metadata"]["_skybuild_workflow"]


def test_explicit_deferral_keeps_new_owner_next_action():
    before, values = state(Place.DEFERRED)
    values["next_action"] = "Check the new milestone"
    synchronize_token(before, values, operation="defer", reason="New owner decision")
    token = TaskToken.from_dict(values["metadata"]["_skybuild_workflow"]["petri"]["token"])
    assert token.next_action == "Check the new milestone"


def test_owner_control_counts_held_claim_without_changing_worker_context():
    from skybuild.contracts import Principal
    from test_petri_store import token_task
    task = token_task(TaskToken("project", "task", Place.WORKING, input_generation=7))
    class Connection:
        def execute(self, query, parameters):
            self.result = {"held": True} if "task_claims" in query else {"input_generation": 7}
            return self
        def fetchone(self):
            return self.result
    original = {"effects_resolved": True, "current_inputs": True}
    checked = Store("unused", "unused")._workflow_control_context(Connection(), Principal("owner", True, {}), task, original)
    assert checked["effects_resolved"] is False
    assert original["effects_resolved"] is True
