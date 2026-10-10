"""Cross-project workflow acceptance without external effects or a database."""
from dataclasses import replace
from itertools import permutations

import pytest

from skybuild.contracts import DomainError
from skybuild.workflow import Place, ResultState, TaskWorkflow, ValidationStage
from test_workflow_behavior import context, event, result, token


@pytest.mark.parametrize("order", tuple(permutations((ResultState.PASSED, ResultState.FAILED))))
@pytest.mark.parametrize("stage", tuple(ValidationStage))
def test_failure_wins_reordered_results_and_keeps_other_project_unchanged(order, stage):
    """The same local task ID never identifies work in another project."""
    workflow = TaskWorkflow()
    current = replace(token(), project_id="skybuild")
    other = replace(current, project_id="example-project")
    untouched = other.to_dict()
    for state in order:
        request = event(current, "validation_result", result=result(current, stage, state).to_dict())
        if current.place == Place.READY:
            with pytest.raises(DomainError):
                workflow.apply(current, request, context(current))
        else:
            current = workflow.apply(current, request, context(current))
    assert current.place == Place.READY
    assert current.faults
    assert current.evidence[-1].state == ResultState.FAILED
    assert other.to_dict() == untouched
    assert (current.project_id, current.task_id) != (other.project_id, other.task_id)
    with pytest.raises(DomainError):
        workflow.apply(other, event(other, "validation_result", result=result(current, stage).to_dict()), context(other))


def test_control_during_unknown_publication_retains_token_and_blocks_new_work():
    """Unknown external outcomes keep ownership until trusted reconciliation."""
    workflow = TaskWorkflow()
    current = token(Place.INTEGRATING)
    pending = workflow.apply(current, event(current, "hold", reason="Owner stop"),
                             context(current, effects_resolved=False))
    assert pending.place == Place.INTEGRATING
    assert pending.pending_action == "hold"
    assert (pending.attempt_id, pending.claim_fence, pending.bundle_id) == (
        current.attempt_id, current.claim_fence, current.bundle_id)
    assert not {"claim", "freeze", "accept"}.intersection(workflow.enabled(pending, context(pending)))
    held = workflow.apply(pending, event(pending, "hold", reason="Owner stop"), context(pending))
    assert held.place == Place.HOLD
    released = workflow.apply(held, event(held, "release_hold", reason="Reconciled"), context(held))
    assert released.place == Place.READY
    assert released.next_action.startswith("Reassess")


@pytest.mark.parametrize("outcome", ["pending", "unknown", "applied", "rejected"])
def test_verified_integration_observation_preserves_pending_control_and_inputs(outcome):
    current = replace(token(Place.INTEGRATING), pending_action="hold")
    facts = context(current, effects_resolved=False, publication_outcome=outcome)
    after = TaskWorkflow().apply(current, event(current, "integration_progress", reason="Observed publication"), facts)
    assert after.place == Place.INTEGRATING and after.pending_action == "hold"
    assert after.bundle_id == current.bundle_id and after.evidence == current.evidence
    assert after.claim_fence == current.claim_fence and after.attempt_id == current.attempt_id


@pytest.mark.parametrize("field,value", [("integration_observation_verified", False),
    ("bundle_id", "other"), ("publication_outcome", "invented")])
def test_integration_observation_rejects_unverified_or_wrong_bundle(field, value):
    current = token(Place.INTEGRATING)
    with pytest.raises(DomainError):
        TaskWorkflow().apply(current, event(current, "integration_progress", reason="Observed"),
                             context(current, **{field: value}))


def test_verified_exclusion_preserves_current_validation_without_advancing_acceptance():
    current = token(Place.INTEGRATING)
    current = replace(current, evidence=(result(current),))
    after = TaskWorkflow().apply(current, event(current, "exclude_from_bundle", reason="Excluded before dispatch"),
                                 context(current, publication_outcome="unpublished"))
    assert after.place == Place.VALIDATING and after.bundle_id is None
    assert after.evidence == current.evidence
    assert after.input_generation == current.input_generation
    assert after.attempt_id == current.attempt_id and after.claim_fence == current.claim_fence


@pytest.mark.parametrize("change", [{"exclusion_verified": False}, {"validation_verified": False},
    {"effects_resolved": False}, {"publication_outcome": "unknown"}, {"publication_outcome": "applied"},
    {"bundle_id": "other"}])
def test_exclusion_requires_resolved_unpublished_current_bundle(change):
    current = token(Place.INTEGRATING)
    with pytest.raises(DomainError):
        TaskWorkflow().apply(current, event(current, "exclude_from_bundle", reason="Exclude"),
                             context(current, **{**{"publication_outcome": "unpublished"}, **change}))
