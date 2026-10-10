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
