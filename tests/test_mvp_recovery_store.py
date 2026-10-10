"""PostgreSQL regression for a failed attempt followed by explicit rework."""

import pytest

from skybuild.contracts import DomainError
from skybuild.store import Store
from skybuild.workflow import Place, ResultState, ValidationResult, ValidationStage
from test_petri_store import author_receipt, enrolled
from test_store import actors, store


def test_failed_validation_rework_fences_old_attempt_and_preserves_history(store, actors):
    """A replacement attempt starts only after the failed attempt is reconciled."""
    project, people = actors
    task = enrolled(store, people, project, task_id="mvp-recovery-regression")

    first_claim = store.claim_task(people["worker"], project, task["task_id"], task["revision"],
                                   "recovery-first-claim")
    first_working = store.get_task(people["owner"], project, task["task_id"])
    first_token = Store.workflow_token(first_working)
    first_receipt = author_receipt(first_token)
    first_submitted = store.workflow_transition(
        people["worker"], project, task["task_id"], "submit", first_receipt,
        first_working["revision"], "recovery-first-submit")
    failed_token = Store.workflow_token(first_submitted["task"])
    failure = ValidationResult(
        project_id=project,
        task_id=task["task_id"],
        stage=ValidationStage.UNIT_TESTS,
        state=ResultState.FAILED,
        attempt_id=failed_token.attempt_id,
        source_head=failed_token.source_head,
        target_base=failed_token.target_base,
        input_generation=failed_token.input_generation,
        definition_revision=failed_token.definition_revision,
        policy_version=failed_token.policy_version,
        claim_fence=failed_token.claim_fence,
        producer=people["worker"].principal_id,
        check_id="recovery-unit-failure",
        findings=("Injected fixture failure requiring a fresh submission",),
        artifacts=("test-artifact://recovery/failure",),
    )
    failure_body = {"result": failure.to_dict()}
    failed = store.workflow_transition(
        people["worker"], project, task["task_id"], "validation_result", failure_body,
        failed_token.revision, "recovery-first-failure")

    assert failed["token"]["place"] == Place.READY.value
    assert failed["task"]["next_action"] == "Correct the validation fault and submit the task again"
    assert failed["token"]["faults"]

    # A terminal failed attempt releases ownership before any rework can begin.
    released = store.release_claim(
        people["worker"], project, task["task_id"], first_claim["fence"],
        failed["task"]["revision"], "recovery-first-release", reason="Failed attempt is quiescent")
    assert released["held"] is False

    reworked = store.task_action(
        people["owner"], project, task["task_id"], "rework",
        {"reason": "Correct failed unit check", "next_action": "Apply fix and submit fresh output"},
        failed["task"]["revision"], "recovery-rework")
    reworked_token = Store.workflow_token(reworked)
    assert reworked_token.place == Place.READY
    assert reworked_token.input_generation > failed_token.input_generation
    assert reworked["next_action"] == "Apply fix and submit fresh output"

    second_claim = store.claim_task(people["worker"], project, task["task_id"], reworked["revision"],
                                    "recovery-second-claim")
    second_working = store.get_task(people["owner"], project, task["task_id"])
    second_token = Store.workflow_token(second_working)
    assert second_claim["fence"] > first_claim["fence"]
    assert second_token.attempt_id != first_token.attempt_id
    assert second_token.claim_fence == second_claim["fence"]
    assert second_token.input_generation == reworked_token.input_generation
    assert second_working["next_action"] == "Apply fix and submit fresh output"

    current_receipt = author_receipt(second_token)
    for field in ("attempt_id", "claim_fence", "input_generation"):
        stale_receipt = dict(current_receipt)
        stale_receipt[field] = first_receipt[field]
        with pytest.raises(DomainError) as caught:
            store.workflow_transition(
                people["worker"], project, task["task_id"], "submit", stale_receipt,
                second_working["revision"], "recovery-stale-" + field)
        assert caught.value.code == "stale_evidence"
    assert store.get_task(people["owner"], project, task["task_id"]) == second_working

    # Replaying the exact failure operation returns its stored response only.
    failure_event_count = store.task_history(people["owner"], project, task["task_id"])
    replayed_failure = store.workflow_transition(
        people["worker"], project, task["task_id"], "validation_result", failure_body,
        failed_token.revision, "recovery-first-failure")
    assert replayed_failure == failed
    assert store.get_task(people["owner"], project, task["task_id"]) == second_working
    assert len(store.task_history(people["owner"], project, task["task_id"])) == len(failure_event_count)

    fresh_receipt = {
        **current_receipt,
        "source_head": "c" * 40,
        "target_base": "b" * 40,
        "source_branch": "refs/heads/task/mvp-recovery-regression",
    }
    accepted_for_validation = store.workflow_transition(
        people["worker"], project, task["task_id"], "submit", fresh_receipt,
        second_working["revision"], "recovery-second-submit")
    assert accepted_for_validation["token"]["place"] == Place.VALIDATING.value
    history_after_submit = store.task_history(people["owner"], project, task["task_id"])
    replayed_submit = store.workflow_transition(
        people["worker"], project, task["task_id"], "submit", fresh_receipt,
        second_working["revision"], "recovery-second-submit")
    assert replayed_submit == accepted_for_validation
    assert len(store.task_history(people["owner"], project, task["task_id"])) == len(history_after_submit)

    operations = [event["operation"] for event in history_after_submit]
    assert operations.count("workflow.validation_result") == 1
    assert operations.count("workflow.submit") == 2
    assert operations.count("rework") == 1
    claims = store.claim_history(people["owner"], project, task["task_id"])
    assert [event["action"] for event in claims] == ["claim", "release", "claim"]
    assert [event["after_state"]["fence"] for event in claims] == [1, 1, 2]
