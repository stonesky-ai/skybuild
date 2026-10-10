"""Connect qualified integration producers to the guarded task transaction.

This module grants no publication authority. A deployment must supply a verifier
which checks durable producer evidence; an unqualified publisher fails closed.
"""
from .contracts import DomainError
from .workflow import Place, ResultState, _result_current


def satisfactory_validation(token):
    """Require current satisfactory evidence for every configured stage."""
    if not token.requirements:
        return False
    for stage in token.requirements:
        results = [value for value in token.evidence
                   if value.stage == stage and _result_current(token, value)]
        if not results or any(value.state not in {ResultState.PASSED, ResultState.NOT_APPLICABLE}
                              for value in results):
            return False
        if any(value.state == ResultState.NOT_APPLICABLE and not value.policy_reason for value in results):
            return False
    return True


def selection_fault(task, head, base):
    """Return a Petri exclusion reason, or None. Preserve legacy selection."""
    from .store import Store
    if not Store._petri(task):
        if "petri" in task.get("metadata", {}).get("_skybuild_workflow", {}):
            return "Petri workflow schema is unsupported"
        return None
    token = Store.workflow_token(task)
    if (token.place != Place.VALIDATING or token.pending_action or token.superseded
            or token.source_head != head or token.target_base != base
            or not satisfactory_validation(token)):
        return "Petri task lacks current satisfactory validation for the frozen inputs"
    return None


class IntegrationWorkflow:
    """Internal adapter. The verifier is deployment code, never request data.

    verify_receipt(connection, task, context, evidence) checks trusted producer
    records and returns checked context facts under the Store task locks.
    No default verifier trusts subprocess returns or caller booleans.
    """

    def __init__(self, store, verify_receipt=None):
        self.store = store
        self.verify_receipt = verify_receipt

    def transition(self, principal, project_id, task_id, event, *, evidence,
                   expected_revision, idempotency_key, reason=None):
        if event not in {"freeze", "integration_failure", "accept", "integration_progress", "exclude_from_bundle"}:
            raise DomainError("validation", "Unsupported integration event", 422)
        if self.verify_receipt is None:
            raise DomainError("workflow_conflict", "Integration producer is not qualified", 409)
        body = {} if reason is None else {"reason": reason}

        def verify(connection, before, context, supplied):
            from .store import Store
            token = Store.workflow_token(before)
            if event in {"freeze", "accept", "exclude_from_bundle"} and not satisfactory_validation(token):
                raise DomainError("stale_evidence", "Required validation is not current and satisfactory", 409)
            if event == "freeze" and context.get("dependencies_satisfied") is not True:
                raise DomainError("workflow_conflict", "Current dependencies are required for bundle freeze", 409)
            facts = self.verify_receipt(connection, before, context, supplied)
            if not isinstance(facts, dict):
                raise DomainError("workflow_conflict", "Integration receipt was not verified", 409)
            if supplied.get("outcome") == "unknown" and event != "integration_progress":
                raise DomainError("workflow_conflict", "Publication is unresolved; retain integration ownership", 409)
            if event == "freeze":
                facts = {**facts, "validation_verified": True}
            return facts

        return self.store.verified_workflow_transition(
            principal, project_id, task_id, event, body, expected_revision, idempotency_key,
            evidence=evidence, verifier=verify)
