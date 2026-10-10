"""Validation and owner controls preserve task identity and effect ownership."""
from dataclasses import replace
from datetime import datetime, timezone
from itertools import product

import pytest

from skybuild.contracts import DomainError
from skybuild.workflow import (
    Place, ResultState, TaskToken, TaskWorkflow, TRANSITIONS, ValidationResult, ValidationStage,
)


def token(place=Place.VALIDATING):
    return TaskToken("project", "TASK-1", place=place, attempt_id="attempt-1", claim_fence=2,
                     source_head="head", target_base="base", definition_revision=3,
                     input_generation=4, policy_version="policy", bundle_id="bundle-1")


def context(current, **changes):
    facts = {name: getattr(current, name) for name in
             ("source_head", "target_base", "definition_revision", "input_generation", "policy_version")}
    facts.update(current_inputs=True, effects_resolved=True, result_authorized=True,
                 control_authorized=True, failure_confirmed=True,
                 now=datetime(2026, 10, 10, tzinfo=timezone.utc))
    facts.update(changes)
    return facts


def event(current, name, **details):
    return dict(event=name, operation_id="operation-1", expected_revision=current.revision, **details)


def result(current, stage=ValidationStage.UNIT_TESTS, state=ResultState.PASSED, **changes):
    values = {name: getattr(current, name) for name in
              ("project_id", "task_id", "attempt_id", "claim_fence", "source_head", "target_base",
               "definition_revision", "input_generation", "policy_version")}
    values.update(stage=stage, state=state, producer="checker", check_id=stage.value)
    values.update(changes)
    return ValidationResult(**values)


def apply(current, name, facts=None, **details):
    return TaskWorkflow().apply(current, event(current, name, **details), facts or context(current))


CONTROLS = tuple(spec for spec in TRANSITIONS if spec.event not in {"claim", "submit", "freeze", "accept"})


@pytest.mark.parametrize("place,spec", list(product(Place, CONTROLS)))
def test_every_control_source(place, spec):
    current = token(place)
    details = {"result": result(current).to_dict()} if spec.event == "validation_result" else {"reason": "Owner decision"}
    if spec.event == "defer":
        details["until"] = "2030-01-01T12:00:00-06:00"
    if place not in spec.sources:
        with pytest.raises(DomainError):
            apply(current, spec.event, **details)
    else:
        after = apply(current, spec.event, **details)
        assert after.place == (spec.destination or place)
        assert after.revision == current.revision + 1
        assert after.task_id == current.task_id
        assert after.attempt_id == current.attempt_id
        assert after.claim_fence == current.claim_fence
        assert after.bundle_id == current.bundle_id


@pytest.mark.parametrize("stage", list(ValidationStage))
@pytest.mark.parametrize("state", [ResultState.PENDING, ResultState.RUNNING, ResultState.PASSED,
                                   ResultState.FAILED, ResultState.NOT_APPLICABLE])
def test_all_stage_results(stage, state):
    current = token()
    recorded = result(current, stage, state, policy_reason="Policy permits omission" if state == ResultState.NOT_APPLICABLE else None)
    after = apply(current, "validation_result", result=recorded.to_dict())
    assert after.evidence == (recorded,)
    assert after.place == (Place.READY if state == ResultState.FAILED else Place.VALIDATING)
    if state == ResultState.FAILED:
        assert after.faults and after.blocker and after.next_action
        with pytest.raises(DomainError):
            apply(after, "validation_result", result=result(after).to_dict())


def test_independent_results_merge_and_replaced_result_remains_in_journal():
    current = token()
    first = result(current, state=ResultState.RUNNING)
    current = apply(current, "validation_result", result=first.to_dict())
    second = result(current, ValidationStage.SCANS)
    current = apply(current, "validation_result", result=second.to_dict())
    request = event(current, "validation_result", result=result(current).to_dict())
    after = TaskWorkflow().apply(current, request, context(current))
    assert len(after.evidence) == 2
    assert all(item.state == ResultState.PASSED for item in after.evidence)
    assert current.evidence[0].state == ResultState.RUNNING
    assert TaskWorkflow.journal_facts(current, after, request)["result"] == request["result"]


@pytest.mark.parametrize("field,value", [("project_id", "other"), ("task_id", "TASK-2"),
    ("attempt_id", "old"), ("claim_fence", 1), ("source_head", "old"), ("target_base", "old"),
    ("definition_revision", 2), ("input_generation", 3), ("policy_version", "old")])
def test_stale_results_cannot_change_token(field, value):
    current = token()
    with pytest.raises(DomainError):
        apply(current, "validation_result", result=result(current, **{field: value}).to_dict())
    assert current.revision == 0 and current.evidence == ()


@pytest.mark.parametrize("name,place", [("hold", Place.INTEGRATING), ("defer", Place.WORKING)])
def test_pending_control_preserves_ownership_and_finishes_after_effect_resolution(name, place):
    current = token(place)
    details = dict(reason="Wait for owner", responsible="owner")
    if name == "defer":
        details["milestone_task_id"] = "TASK-2"
    pending = apply(current, name, context(current, effects_resolved=False), **details)
    assert pending.place == place and pending.pending_action == name
    assert pending.interrupted_place == place
    assert pending.attempt_id == current.attempt_id
    assert "freeze" not in TaskWorkflow().enabled(pending, context(pending))
    after = apply(pending, name, **details)
    assert after.place == (Place.HOLD if name == "hold" else Place.DEFERRED)
    assert after.pending_action is None
    assert after.interrupted_place == place


def test_terminal_result_is_retained_during_pending_hold():
    pending = apply(token(), "hold", context(token(), effects_resolved=False), reason="Stop")
    after = apply(pending, "validation_result", context(pending, effects_resolved=False),
                  result=result(pending).to_dict())
    assert after.place == Place.VALIDATING and after.pending_action == "hold" and after.evidence


@pytest.mark.parametrize("name,place", [("validation_failure", Place.VALIDATING),
    ("integration_failure", Place.INTEGRATING), ("work_failure", Place.WORKING)])
def test_confirmed_failures_preserve_unresolved_ownership(name, place):
    current = token(place)
    after = apply(current, name, context(current, effects_resolved=False), reason="Confirmed fault")
    assert after.place == Place.READY and after.attempt_id == current.attempt_id
    assert after.bundle_id == current.bundle_id and after.claim_fence == current.claim_fence
    assert after.faults == ("Confirmed fault",)
    assert "claim" not in TaskWorkflow().enabled(after, context(after, effects_resolved=False))


def test_safe_abandon_requires_resolved_effects_without_confirmed_failure():
    current = token(Place.WORKING)
    with pytest.raises(DomainError):
        apply(current, "work_failure", context(current, effects_resolved=False, failure_confirmed=False), reason="Release")
    assert apply(current, "work_failure", context(current, failure_confirmed=False), reason="Release").place == Place.READY


@pytest.mark.parametrize("name,place", [("release_hold", Place.HOLD), ("resume_deferred", Place.DEFERRED),
                                      ("reopen", Place.DONE)])
def test_release_resume_reopen_require_effect_resolution(name, place):
    current = token(place)
    with pytest.raises(DomainError):
        apply(current, name, context(current, effects_resolved=False), reason="Resume")
    after = apply(current, name, reason="Resume")
    assert after.place == Place.READY and "Reassess" in after.next_action


def test_due_resume_uses_trusted_trigger_without_owner_authority():
    current = token(Place.DEFERRED)
    assert apply(current, "resume_deferred", context(current, control_authorized=False, resume_due=True), reason="Due").place == Place.READY
    with pytest.raises(DomainError):
        apply(current, "resume_deferred", context(current, control_authorized=False, resume_due=False), reason="Due")


@pytest.mark.parametrize("until", ["2030-01-01", "bad", "2030-01-01T10:00:00"])
def test_defer_requires_timezone(until):
    with pytest.raises(DomainError):
        apply(token(), "defer", reason="Later", until=until)


def test_reopen_marks_evidence_stale_and_increments_generation():
    current = token(Place.DONE)
    current = replace(current, evidence=(result(current),))
    after = apply(current, "reopen", reason="Acceptance changed")
    assert after.input_generation == current.input_generation + 1
    assert after.evidence[0].state == ResultState.STALE
    assert current.evidence[0].state == ResultState.PASSED


@pytest.mark.parametrize("spec", CONTROLS)
def test_superseded_task_rejects_every_control(spec):
    current = replace(token(spec.sources[0]), superseded=True)
    with pytest.raises(DomainError):
        apply(current, spec.event, reason="Restore")


def test_missing_authority_and_missing_policy_reason_fail_closed():
    current = token()
    with pytest.raises(DomainError):
        apply(current, "validation_result", context(current, result_authorized=False), result=result(current).to_dict())
    with pytest.raises(DomainError):
        apply(current, "validation_result", result=result(current, state=ResultState.NOT_APPLICABLE).to_dict())
    with pytest.raises(DomainError):
        apply(current, "hold", context(current, control_authorized=False), reason="Stop")


@pytest.mark.parametrize("until", ["2026-10-10T00:00:00+00:00", "2026-10-09T23:59:59+00:00"])
def test_new_deferral_rejects_exact_now_and_past(until):
    with pytest.raises(DomainError, match="already passed"):
        apply(token(), "defer", reason="Later", until=until)


def test_future_deferral_uses_offset_and_trusted_clock():
    current = token()
    assert apply(current, "defer", reason="Later", until="2026-10-09T19:00:00-06:00").place == Place.DEFERRED
    with pytest.raises(DomainError):
        apply(current, "defer", context(current, now=None), reason="Later", until="2030-01-01T00:00:00+00:00")


def test_accepted_pending_deferral_can_finish_after_trigger_but_cannot_change_to_past():
    current = token(Place.INTEGRATING)
    until = "2026-10-10T01:00:00+00:00"
    pending = apply(current, "defer", context(current, effects_resolved=False), reason="Later", until=until)
    later = context(pending, now=datetime(2026, 10, 11, tzinfo=timezone.utc))
    after = apply(pending, "defer", later, reason="Later", until=until)
    assert after.place == Place.DEFERRED and after.deferred_until == until
    with pytest.raises(DomainError):
        apply(pending, "defer", later, reason="Changed", until="2026-10-10T02:00:00+00:00")
    with pytest.raises(DomainError):
        apply(pending, "defer", later, reason="Different decision", until=until)
    assert apply(after, "update_control", context(after, now=later["now"]), reason="Same trigger", until=until).place == Place.DEFERRED
    with pytest.raises(DomainError):
        apply(after, "update_control", context(after, now=later["now"]), reason="Changed", until="2026-10-10T03:00:00+00:00")


@pytest.mark.parametrize("length,prior_evidence", [(2500, False), (4000, False), (4000, True)])
def test_large_valid_findings_cannot_prevent_failure_recovery(length, prior_evidence):
    current = token()
    if prior_evidence:
        current = replace(current, evidence=(result(current, ValidationStage.SCANS, findings=("old" * 1300,)),))
    findings = ("a" * length, "b" * length)
    failed = result(current, state=ResultState.FAILED, findings=findings)
    request = event(current, "validation_result", result=failed.to_dict())
    after = TaskWorkflow().apply(current, request, context(current))
    assert after.place == Place.READY and len(after.blocker) == 512
    assert after.findings == (after.blocker,)
    assert failed in after.evidence
    assert TaskWorkflow.journal_facts(current, after, request)["result"]["findings"] == list(findings)


def test_failure_projection_bounds_findings_count_and_links_exact_journal_operation():
    current = token()
    current = replace(current, evidence=(result(current, ValidationStage.SCANS, findings=("old" * 1300,)),))
    findings = tuple(str(number) + "x" * 110 for number in range(100))
    failed = result(current, state=ResultState.FAILED, findings=findings)
    request = event(current, "validation_result", result=failed.to_dict())
    after = TaskWorkflow().apply(current, request, context(current))
    assert after.place == Place.READY
    assert len(after.evidence) == 1 and len(after.evidence[0].findings) == 1
    assert after.evidence[0].artifacts == ("workflow-event:operation-1",)
    assert after.findings == (after.blocker,)
    assert TaskWorkflow.journal_facts(current, after, request)["result"] == failed.to_dict()
