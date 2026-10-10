"""Pure normal-path workflow checks without claims, storage or dispatch."""

from dataclasses import replace
from itertools import product

import pytest

from skybuild.contracts import DomainError
from skybuild.workflow import Place, TaskToken, TaskWorkflow, TRANSITIONS


def token(place=Place.READY):
    return TaskToken("project", "TASK-1", place=place, source_head="abc", target_base="def",
                     definition_revision=3, input_generation=2, policy_version="policy-1")


def context(current):
    return dict(source_head=current.source_head, target_base=current.target_base,
                definition_revision=current.definition_revision, input_generation=current.input_generation,
                policy_version=current.policy_version, current_inputs=True, effects_resolved=True,
                dependencies_satisfied=True, admission_permitted=True, claim_live=True,
                attempt_id="attempt-1", claim_fence=1, responsible="worker-1",
                submission_verified=True, validation_verified=True, integration_fixed=True,
                bundle_id="bundle-1", publication_required=True, acceptance_verified=True,
                publication_verified=True, task_included=True)


def event(current, name):
    return dict(event=name, operation_id=f"operation-{name}", expected_revision=current.revision)


def prepared(place):
    current = token(place)
    if place != Place.READY:
        current = replace(current, attempt_id="attempt-1", claim_fence=1)
    if place == Place.INTEGRATING:
        current = replace(current, bundle_id="bundle-1")
    return current


def test_normal_path_conserves_identity_and_records_exact_facts():
    workflow = TaskWorkflow()
    current = token()
    for spec in TRANSITIONS:
        before = current
        facts = context(before)
        request = event(before, spec.event)
        assert workflow.enabled(before, facts) == (spec.event,)
        current = workflow.apply(before, request, facts)
        assert current.place == spec.destination
        assert (current.project_id, current.task_id) == (before.project_id, before.task_id)
        assert current.revision == before.revision + 1
        assert current.definition_revision == before.definition_revision
        assert current.input_generation == before.input_generation
        assert before.place in spec.sources
        journal = workflow.journal_facts(before, current, request)
        assert journal["from_place"] == before.place.value
        assert journal["to_place"] == current.place.value
        assert journal["operation_id"] == request["operation_id"]
        assert journal["revision"] == current.revision
        assert journal["claim_fence"] == 1
    assert current.place == Place.DONE
    assert current.attempt_id == "attempt-1"
    assert current.bundle_id == "bundle-1"
    assert workflow.enabled(current, context(current)) == ()


@pytest.mark.parametrize("place,name", list(product(Place, [spec.event for spec in TRANSITIONS])))
def test_transition_table_rejects_each_forbidden_source(place, name):
    current = prepared(place)
    spec = next(item for item in TRANSITIONS if item.event == name)
    if place in spec.sources:
        assert TaskWorkflow().apply(current, event(current, name), context(current)).place == spec.destination
    else:
        with pytest.raises(DomainError) as error:
            TaskWorkflow().apply(current, event(current, name), context(current))
        assert error.value.status_code == 409


@pytest.mark.parametrize("name", [spec.event for spec in TRANSITIONS])
@pytest.mark.parametrize("field", ["source_head", "target_base", "definition_revision", "input_generation", "policy_version"])
def test_every_transition_rejects_changed_or_missing_input(name, field):
    spec = next(item for item in TRANSITIONS if item.event == name)
    current = prepared(spec.sources[0])
    facts = context(current)
    facts.pop(field)
    assert TaskWorkflow().enabled(current, facts) == ()
    with pytest.raises(DomainError):
        TaskWorkflow().apply(current, event(current, name), facts)
    facts[field] = 99 if field in {"definition_revision", "input_generation"} else "changed"
    with pytest.raises(DomainError):
        TaskWorkflow().apply(current, event(current, name), facts)


@pytest.mark.parametrize("name,field", [
    ("claim", "dependencies_satisfied"), ("claim", "admission_permitted"), ("claim", "claim_live"),
    ("submit", "claim_live"), ("submit", "submission_verified"),
    ("freeze", "validation_verified"), ("freeze", "integration_fixed"),
    ("accept", "acceptance_verified"), ("accept", "publication_verified"), ("accept", "task_included"),
])
@pytest.mark.parametrize("value", [False, None, 1, "true"])
def test_guard_facts_require_explicit_true(name, field, value):
    spec = next(item for item in TRANSITIONS if item.event == name)
    current = prepared(spec.sources[0])
    facts = {**context(current), field: value}
    assert TaskWorkflow().enabled(current, facts) == ()
    with pytest.raises(DomainError):
        TaskWorkflow().apply(current, event(current, name), facts)


@pytest.mark.parametrize("name", [spec.event for spec in TRANSITIONS])
@pytest.mark.parametrize("change", [{"superseded": True}, {"pending_action": "hold"}])
def test_retired_or_pending_tasks_cannot_take_normal_path(name, change):
    spec = next(item for item in TRANSITIONS if item.event == name)
    current = replace(prepared(spec.sources[0]), **change)
    assert TaskWorkflow().enabled(current, context(current)) == ()


def test_submit_rejects_old_attempt_and_fence():
    current = prepared(Place.WORKING)
    for changed in ({"attempt_id": "old"}, {"claim_fence": 2}, {"claim_fence": True}):
        with pytest.raises(DomainError):
            TaskWorkflow().apply(current, event(current, "submit"), {**context(current), **changed})


def test_claim_rejects_invalid_fence_and_owner():
    current = token()
    for changed in ({"claim_fence": 0}, {"claim_fence": True}, {"claim_fence": 2**63},
                    {"responsible": ""}, {"responsible": "bad\x00owner"}):
        with pytest.raises(DomainError):
            TaskWorkflow().apply(current, event(current, "claim"), {**context(current), **changed})


def test_no_publication_path_requires_explicit_policy_reason():
    workflow = TaskWorkflow()
    current = prepared(Place.VALIDATING)
    facts = {**context(current), "publication_required": False}
    with pytest.raises(DomainError):
        workflow.apply(current, event(current, "freeze"), facts)
    facts["policy_reason"] = "Documentation acceptance does not require publication"
    current = workflow.apply(current, event(current, "freeze"), facts)
    assert current.bundle_id is None
    facts.update(acceptance_verified=True)
    current = workflow.apply(current, event(current, "accept"), facts)
    assert current.place == Place.DONE


def test_unknown_publication_and_wrong_bundle_cannot_finish():
    current = prepared(Place.INTEGRATING)
    for changed in ({"publication_verified": False}, {"bundle_id": "other"}, {"publication_required": None}):
        with pytest.raises(DomainError):
            TaskWorkflow().apply(current, event(current, "accept"), {**context(current), **changed})


@pytest.mark.parametrize("change", [{"event": "teleport"}, {"expected_revision": 7},
                                      {"operation_id": ""}, {"expected_revision": True},
                                      {"destination": "done"}, {"reason": "caller detail"}])
def test_event_rejects_stale_revision_unknown_fields_and_direct_movement(change):
    current = token()
    with pytest.raises(DomainError):
        TaskWorkflow().apply(current, {**event(current, "claim"), **change}, context(current))


def test_journal_rejects_cross_task_or_nonsequential_changes():
    before = token()
    for after in (replace(before, task_id="TASK-2", revision=1), replace(before, revision=2)):
        with pytest.raises(DomainError):
            TaskWorkflow.journal_facts(before, after, event(before, "claim"))



def test_verified_no_publication_reason_survives_token_and_journal():
    workflow = TaskWorkflow()
    current = prepared(Place.VALIDATING)
    reason = "Policy accepts this task without publication"
    facts = {**context(current), "publication_required": False, "policy_reason": reason}
    for name in ("freeze", "accept"):
        before = current
        request = event(before, name)
        current = workflow.apply(before, request, facts)
        assert current.policy_reason == reason
        assert TaskToken.from_dict(current.to_dict()).policy_reason == reason
        journal = workflow.journal_facts(before, current, request)
        assert journal["policy_reason"] == reason
        assert journal["acceptance_mode"] == "without_publication"


def test_publication_path_clears_old_exemption_and_records_mode():
    workflow = TaskWorkflow()
    for place, name in ((Place.VALIDATING, "freeze"), (Place.INTEGRATING, "accept")):
        before = replace(prepared(place), policy_reason="Old exemption")
        request = event(before, name)
        after = workflow.apply(before, request, context(before))
        assert after.policy_reason is None
        assert workflow.journal_facts(before, after, request)["acceptance_mode"] == "publication"


def test_caller_cannot_supply_no_publication_policy_reason():
    current = prepared(Place.VALIDATING)
    with pytest.raises(DomainError) as error:
        TaskWorkflow().apply(current, {**event(current, "freeze"), "policy_reason": "Forged"}, context(current))
    assert error.value.status_code == 422
