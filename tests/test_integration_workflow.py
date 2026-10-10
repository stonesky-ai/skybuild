"""Qualified integration events preserve exact inputs and acceptance authority."""
import pytest

from skybuild.completion import (completion_without_publication, current_completion, completion_change,
                                 freeze_without_publication_policy)
from skybuild.contracts import DomainError
from skybuild.integration_workflow import IntegrationWorkflow, selection_fault
from skybuild.workflow import TaskToken, ValidationResult, ValidationStage, ResultState, Place, TaskWorkflow


def task(place=Place.VALIDATING):
    result = ValidationResult("project", "task", ValidationStage.CODE_REVIEW, ResultState.PASSED,
                              source_head="a" * 40, target_base="b" * 40, policy_version="policy@1",
                              producer="independent-reviewer")
    token = TaskToken("project", "task", place, source_head="a" * 40, target_base="b" * 40,
                      policy_version="policy@1", policy_reason="Research requires no publication",
                      requirements=(ValidationStage.CODE_REVIEW,), evidence=(result,), revision=1)
    return {"project_id": "project", "task_id": "task", "title": "Research", "priority": 2,
            "description": "Research", "dependencies": [], "architecture_refs": [],
            "acceptance_criteria": ["Complete research"], "responsible": "owner", "next_action": "Accept",
            "blocker": None, "revision": 1, "status": "in-progress",
            "metadata": {"_skybuild_workflow": {"generation": 0, "petri": {
                "schema_version": 1, "token": token.to_dict(), "acceptance_policy": {
                    "version": "policy@1", "publication_required": False,
                    "reason": token.policy_reason, "requirements": ["code_review"], "review_required": True}}}}}


def context(value):
    from skybuild.store import Store
    token = Store.workflow_token(value)
    return {**{name: getattr(token, name) for name in
               ("source_head", "target_base", "definition_revision", "input_generation", "policy_version")},
            "current_inputs": True, "effects_resolved": True, "control_authorized": True,
            "dependencies_satisfied": True,
            "publication_required": False, "policy_reason": token.policy_reason,
            "publication_policy_version": "policy@1",
            "acceptance_policy": {"version": "policy@1", "publication_required": False,
                "reason": token.policy_reason, "requirements": ["code_review"], "review_required": True}}


def evidence():
    return {"kind": "without_publication", "reason": "Accepted research", "generation": 0,
            "author": "author", "policy_ref": "policy@1", "acceptance": [
                {"criterion": "Complete research", "evidence_ref": "research/report@1"}]}


class Store:
    """Fake transactional wrapper. Producer verification receives trusted facts."""
    def __init__(self, value):
        self.value = value
        self.calls = 0

    def verified_workflow_transition(self, principal, project, task_id, event, body,
                                     revision, key, *, evidence, verifier):
        self.calls += 1
        if principal != "admin":
            raise DomainError("authorization", "Admin required", 403)
        from skybuild.store import Store as ActualStore
        before = ActualStore.workflow_token(self.value)
        facts = context(self.value)
        facts.update(verifier(None, self.value, facts, evidence))
        after = TaskWorkflow().apply(before, {"event": event, "operation_id": key,
                                              "expected_revision": revision, **body}, facts)
        self.value["metadata"]["_skybuild_workflow"]["petri"]["token"] = after.to_dict()
        self.value["revision"] = after.revision
        return after


def test_selection_preserves_legacy_and_rejects_stale_or_incomplete_petri():
    value = task()
    assert selection_fault(value, "a" * 40, "b" * 40) is None
    assert selection_fault(value, "c" * 40, "b" * 40)
    value["metadata"]["_skybuild_workflow"]["petri"]["token"]["evidence"] = []
    assert selection_fault(value, "a" * 40, "b" * 40)
    assert selection_fault({"status": "ready"}, "a" * 40, "b" * 40) is None


def test_fake_qualified_freeze_failure_and_unknown_publication():
    store = Store(task())
    adapter = IntegrationWorkflow(store, lambda *args: {"integration_fixed": True,
                                 "publication_required": True, "bundle_id": "bundle-1"})
    after = adapter.transition("admin", "project", "task", "freeze", evidence={"receipt": "frozen@1"},
                               expected_revision=1, idempotency_key="freeze-1")
    assert after.place == Place.INTEGRATING and after.bundle_id == "bundle-1"
    unknown = IntegrationWorkflow(store, lambda *args: {})
    with pytest.raises(DomainError, match="Publication is unresolved"):
        unknown.transition("admin", "project", "task", "integration_failure", reason="Unknown response",
                           evidence={"outcome": "unknown"}, expected_revision=2, idempotency_key="unknown")
    assert store.value["revision"] == 2
    failed = IntegrationWorkflow(store, lambda *args: {"failure_confirmed": True})
    result = failed.transition("admin", "project", "task", "integration_failure", reason="Confirmed gate fault",
                               evidence={}, expected_revision=2, idempotency_key="failure")
    assert result.place == Place.READY and "Confirmed gate fault" in result.faults


def test_unqualified_and_denied_producers_cannot_transition():
    store = Store(task())
    with pytest.raises(DomainError, match="not qualified"):
        IntegrationWorkflow(store).transition("admin", "project", "task", "freeze", evidence={},
                                              expected_revision=1, idempotency_key="unqualified")
    assert store.calls == 0
    with pytest.raises(DomainError) as error:
        IntegrationWorkflow(store, lambda *args: {}).transition("worker", "project", "task", "freeze",
                       evidence={}, expected_revision=1, idempotency_key="denied")
    assert error.value.status_code == 403


def test_without_publication_completion_is_current_without_invented_git_evidence():
    before = task(Place.INTEGRATING)
    changes = completion_without_publication(before, evidence(), "admin", context(before))
    after = {**before, **changes}
    after["metadata"]["_skybuild_workflow"]["petri"]["token"]["place"] = "done"
    assert current_completion(after)
    assert "publication" not in after["metadata"]["_skybuild_completion"]
    after["metadata"]["_skybuild_workflow"]["petri"]["token"]["input_generation"] = 1
    assert not current_completion(after)


@pytest.mark.parametrize("change", [
    lambda c: c.pop("acceptance_policy"),
    lambda c: c["acceptance_policy"].update(publication_required=True),
    lambda c: c["acceptance_policy"].update(version="old@1"),
    lambda c: c.update(input_generation=1),
    lambda c: c.update(current_inputs=False),
    lambda c: c["acceptance_policy"].update(requirements=[]),
])
def test_without_publication_rejects_absent_policy_or_stale_inputs(change):
    before = task(Place.INTEGRATING)
    facts = context(before)
    change(facts)
    with pytest.raises(DomainError):
        completion_without_publication(before, evidence(), "admin", facts)


def test_without_publication_rejects_self_review_and_public_completion_exemption():
    before = task(Place.INTEGRATING)
    before["metadata"]["_skybuild_workflow"]["petri"]["token"]["evidence"][0]["producer"] = "author"
    with pytest.raises(DomainError, match="independent review"):
        completion_without_publication(before, evidence(), "admin", context(before))
    with pytest.raises(DomainError):
        completion_change(before, evidence(), "admin")


@pytest.mark.parametrize("change", [
    lambda t: t.update(description="Changed research"),
    lambda t: t["metadata"]["_skybuild_workflow"].update(generation=1),
    lambda t: t["metadata"]["_skybuild_workflow"]["petri"]["token"].update(policy_version="old@1"),
    lambda t: t["metadata"]["_skybuild_workflow"]["petri"]["token"]["evidence"][0].update(producer="author"),
])
def test_without_publication_acceptance_invalidates_on_changed_inputs(change):
    before = task(Place.INTEGRATING)
    after = {**before, **completion_without_publication(before, evidence(), "admin", context(before))}
    after["metadata"]["_skybuild_workflow"]["petri"]["token"]["place"] = "done"
    assert current_completion(after)
    change(after)
    assert not current_completion(after)


def test_accept_cannot_substitute_another_bundle_or_skip_inclusion():
    value = task(Place.INTEGRATING)
    value["metadata"]["_skybuild_workflow"]["petri"]["token"]["bundle_id"] = "actual-bundle"
    for facts in [
        {"publication_required": True, "acceptance_verified": True, "publication_verified": True,
         "task_included": True, "bundle_id": "wrong-bundle"},
        {"publication_required": True, "acceptance_verified": True, "publication_verified": True,
         "task_included": False, "bundle_id": "actual-bundle"},
    ]:
        store = Store(value)
        with pytest.raises(DomainError):
            IntegrationWorkflow(store, lambda *args: facts).transition(
                "admin", "project", "task", "accept", evidence={}, expected_revision=1, idempotency_key="accept")
        assert store.value["revision"] == 1


def test_without_publication_rejects_policy_not_frozen_in_managed_metadata():
    before = task(Place.INTEGRATING)
    before["metadata"]["_skybuild_workflow"]["petri"].pop("acceptance_policy")
    with pytest.raises(DomainError, match="not frozen"):
        completion_without_publication(before, evidence(), "admin", context(before))


def test_internal_freeze_configures_bounded_policy_and_does_not_modify_input():
    before = task()
    before["metadata"]["_skybuild_workflow"]["petri"].pop("acceptance_policy")
    prepared = freeze_without_publication_policy(before, context(before))
    assert prepared["metadata"]["_skybuild_workflow"]["petri"]["acceptance_policy"] == context(before)["acceptance_policy"]
    assert "acceptance_policy" not in before["metadata"]["_skybuild_workflow"]["petri"]
    facts = context(before)
    facts["acceptance_policy"]["version"] = "other@1"
    with pytest.raises(DomainError):
        freeze_without_publication_policy(before, facts)


# These fixtures use only SKYBUILD_TEST_DSN and skip outside a disposable database.
from test_store import store, actors  # noqa: E402,F401


def test_verified_store_freezes_policy_and_accepts_without_publication(store, actors):
    from dataclasses import replace
    from psycopg.types.json import Jsonb
    from skybuild.store import Store as ActualStore
    from test_petri_store import enrolled
    project, people = actors
    before = enrolled(store, people, project)
    original = ActualStore.workflow_token(before)
    result = ValidationResult(project, before["task_id"], ValidationStage.CODE_REVIEW, ResultState.PASSED,
                              input_generation=original.input_generation,
                              definition_revision=original.definition_revision,
                              policy_version="policy@1", producer="independent-reviewer")
    token = replace(original, place=Place.VALIDATING, policy_version="policy@1",
                    requirements=(ValidationStage.CODE_REVIEW,), evidence=(result,))
    # Simulate persisted qualified submission/check inputs; no external effect occurs.
    before["metadata"]["_skybuild_workflow"]["petri"]["token"] = token.to_dict()
    with store._connection() as connection:
        connection.execute("UPDATE tasks SET metadata=%s, status='in-progress' WHERE project_id=%s AND task_id=%s",
                           (Jsonb(before["metadata"]), project, before["task_id"]))
    policy = {"version": "policy@1", "publication_required": False, "reason": "Research requires no publication",
              "requirements": ["code_review"], "review_required": True}
    def qualified(connection, current, facts, receipt):
        assert receipt == {"fixture_receipt": "checked-policy@1"}
        return {"integration_fixed": True, "publication_required": False, "publication_policy_version": "policy@1",
                "policy_reason": policy["reason"], "acceptance_policy": policy}
    adapter = IntegrationWorkflow(store, qualified)
    frozen = adapter.transition(people["owner"], project, before["task_id"], "freeze",
                       evidence={"fixture_receipt": "checked-policy@1"}, expected_revision=before["revision"],
                       idempotency_key="freeze-policy")
    assert frozen["token"]["place"] == "integrating"
    assert frozen["task"]["metadata"]["_skybuild_workflow"]["petri"]["acceptance_policy"] == policy
    body = evidence()
    body.update(generation=frozen["task"]["metadata"]["_skybuild_workflow"]["generation"],
                acceptance=[{"criterion": "Retain ownership history", "evidence_ref": "research/report@1"}])
    def acceptance(connection, current, facts, receipt):
        return {**qualified(connection, current, facts, receipt), "acceptance_verified": True,
                "completion_evidence": body}
    adapter = IntegrationWorkflow(store, acceptance)
    accepted = adapter.transition(people["owner"], project, before["task_id"], "accept",
                         evidence={"fixture_receipt": "checked-policy@1"}, expected_revision=frozen["task"]["revision"],
                         idempotency_key="accept-policy")
    assert current_completion(accepted["task"])
    assert accepted["token"]["place"] == "done"
    assert "publication" not in accepted["task"]["metadata"]["_skybuild_completion"]
    assert adapter.transition(people["owner"], project, before["task_id"], "accept",
                  evidence={"fixture_receipt": "checked-policy@1"}, expected_revision=frozen["task"]["revision"],
                  idempotency_key="accept-policy") == accepted


@pytest.mark.parametrize("place", list(Place))
def test_legacy_completion_route_cannot_bypass_petri_acceptance(store, actors, place):
    from test_petri_store import enrolled
    from test_completion import evidence as publication_evidence
    project, people = actors
    before = enrolled(store, people, project)
    from psycopg.types.json import Jsonb
    before["metadata"]["_skybuild_workflow"]["petri"]["token"]["place"] = place.value
    with store._connection() as connection:
        connection.execute("UPDATE tasks SET metadata=%s WHERE project_id=%s AND task_id=%s",
                           (Jsonb(before["metadata"]), project, before["task_id"]))
    with pytest.raises(DomainError, match="verified workflow acceptance"):
        store.complete_task(people["owner"], project, before["task_id"], publication_evidence(),
                            before["revision"], "bypass")
    assert store.get_task(people["owner"], project, before["task_id"])["revision"] == before["revision"]


def publication_body():
    from test_completion import evidence as legacy_evidence
    body = legacy_evidence()
    body.update(source_head="a" * 40, policy_ref="policy@1",
                acceptance=[{"criterion": "Complete research", "evidence_ref": "research/report@1"}])
    body["checks"][0]["source_head"] = body["source_head"]
    body["review"]["source_head"] = body["source_head"]
    body["publication"].update(source_head=body["source_head"], base_commit="b" * 40)
    return body


@pytest.mark.parametrize("mismatch", ["head", "base", "policy"])
def test_publication_acceptance_rejects_internally_consistent_wrong_petri_inputs(mismatch):
    before = task(Place.INTEGRATING)
    body = publication_body()
    if mismatch == "head":
        body["source_head"] = "c" * 40
        body["checks"][0]["source_head"] = "c" * 40
        body["review"]["source_head"] = "c" * 40
        body["publication"]["source_head"] = "c" * 40
    elif mismatch == "base":
        body["publication"]["base_commit"] = "c" * 40
    else:
        body["policy_ref"] = "other@1"
    with pytest.raises(DomainError, match="current Petri inputs"):
        completion_change(before, body, "admin")


@pytest.mark.parametrize("field,value", [
    ("source_head", "d" * 40), ("target_base", "d" * 40), ("policy_version", "other@1"),
    ("input_generation", 1), ("definition_revision", 1), ("attempt_id", "new-attempt"), ("claim_fence", 2),
])
def test_publication_completion_becomes_stale_when_petri_inputs_change(field, value):
    before = task(Place.INTEGRATING)
    after = {**before, **completion_change(before, publication_body(), "admin")}
    after["metadata"]["_skybuild_workflow"]["petri"]["token"]["place"] = "done"
    assert current_completion(after)
    after["metadata"]["_skybuild_workflow"]["petri"]["token"][field] = value
    assert not current_completion(after)
