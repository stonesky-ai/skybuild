"""Bounded owner attestations for manual code-task completion, without external I/O."""

from copy import deepcopy
import re

from .contracts import DomainError

RESERVED_KEY = "_skybuild_completion"
DEFINITION_FIELDS = ("title", "description", "dependencies", "acceptance_criteria", "architecture_refs")


def _text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 4096 or "\x00" in value:
        raise DomainError("validation", "Evidence requires bounded nonempty text", 422)
    return value


def _object(value, fields):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise DomainError("validation", "Completion evidence has missing or unsupported fields", 422)


def definition(task):
    return {field: deepcopy(task[field]) for field in DEFINITION_FIELDS}


def generation(task):
    workflow = task["metadata"].get("_skybuild_workflow", {})
    value = workflow.get("generation", 0) if isinstance(workflow, dict) else None
    if type(value) is not int or value < 0:
        raise DomainError("workflow_conflict", "Workflow generation is invalid", 409)
    return value


def completion_change(task, body, actor):
    """Validate a trusted owner's statement; never claim an external verification."""
    _object(body, ("reason", "generation", "source_head", "author", "policy_ref", "acceptance", "checks", "review", "publication"))
    for field in ("reason", "author", "policy_ref"):
        _text(body[field])
    head = body["source_head"]
    if not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head):
        raise DomainError("validation", "Source head must be a full lowercase Git object ID", 422)
    if type(body["generation"]) is not int or body["generation"] != generation(task):
        raise DomainError("stale_evidence", "Evidence generation does not match current task", 409)
    if task["status"] in {"done", "deferred", "superseded"}:
        raise DomainError("workflow_conflict", "Resume or reopen task before recording completion", 409)
    acceptance = body["acceptance"]
    if not task["acceptance_criteria"] or not isinstance(acceptance, list) or len(acceptance) != len(task["acceptance_criteria"]):
        raise DomainError("validation", "Completion requires evidence for every acceptance criterion", 422)
    for criterion, evidence in zip(task["acceptance_criteria"], acceptance):
        _object(evidence, ("criterion", "evidence_ref"))
        if evidence["criterion"] != criterion:
            raise DomainError("stale_evidence", "Acceptance evidence must match current ordered criteria", 409)
        _text(evidence["evidence_ref"])
    checks = body["checks"]
    if not isinstance(checks, list) or not 1 <= len(checks) <= 100:
        raise DomainError("validation", "Completion requires bounded passing check evidence", 422)
    names = set()
    for check in checks:
        _object(check, ("name", "source_head", "result", "evidence_ref"))
        name = _text(check["name"])
        if name in names:
            raise DomainError("validation", "Check names must be unique", 422)
        names.add(name)
        _text(check["evidence_ref"])
        if check["result"] != "passed" or check["source_head"] != head:
            raise DomainError("stale_evidence", "Checks must pass against the attested source head", 409)
    review = body["review"]
    _object(review, ("reviewer", "session_ref", "source_head", "result", "unresolved_blocking_findings", "evidence_ref"))
    for field in ("reviewer", "session_ref", "evidence_ref"):
        _text(review[field])
    if review["reviewer"] == body["author"]:
        raise DomainError("workflow_conflict", "Author cannot approve their own patch", 409)
    if review["result"] != "passed" or review["source_head"] != head or type(review["unresolved_blocking_findings"]) is not int or review["unresolved_blocking_findings"] != 0:
        raise DomainError("workflow_conflict", "Completion requires current independent review with no blocking findings", 409)
    publication = body["publication"]
    _object(publication, ("source_head", "candidate_commit", "base_commit", "target_commit", "target_ref", "result", "inclusion_evidence_ref"))
    for field in ("candidate_commit", "base_commit", "target_commit"):
        if not isinstance(publication[field], str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", publication[field]):
            raise DomainError("validation", "Publication requires full Git object IDs", 422)
    for field in ("target_ref", "inclusion_evidence_ref"):
        _text(publication[field])
    if publication["source_head"] != head or publication["result"] != "confirmed":
        raise DomainError("workflow_conflict", "Publication must confirm exact task inclusion", 409)
    petri_inputs = _publication_token_binding(task, body)
    evidence = {"schema_version": 1, "kind": "owner_attestation", "actor": actor,
                "input_revision": task["revision"], "definition": definition(task), **deepcopy(body)}
    if petri_inputs is not None:
        evidence["petri_inputs"] = petri_inputs
    metadata = deepcopy(task["metadata"])
    metadata.setdefault("_skybuild_workflow", {"generation": generation(task)})
    metadata[RESERVED_KEY] = evidence
    return {"status": "done", "phase": "done", "next_action": None, "blocker": None, "metadata": metadata}





_PUBLICATION_INPUTS = ("source_head", "target_base", "definition_revision", "input_generation", "policy_version",
                       "attempt_id", "claim_fence")


def _has_petri(task):
    return "petri" in task.get("metadata", {}).get("_skybuild_workflow", {})


def _publication_token_binding(task, evidence, *, current=False):
    """Bind publication evidence to Petri inputs; preserve the legacy contract."""
    if not _has_petri(task):
        return None
    from .store import Store
    from .workflow import Place
    from .integration_workflow import satisfactory_validation
    token = Store.workflow_token(task)
    inputs = {name: getattr(token, name) for name in _PUBLICATION_INPUTS}
    if (token.place != (Place.DONE if current else Place.INTEGRATING)
            or evidence.get("source_head") != token.source_head
            or evidence.get("publication", {}).get("base_commit") != token.target_base
            or evidence.get("policy_ref") != token.policy_version or not token.policy_version
            or evidence.get("generation") != generation(task) or not satisfactory_validation(token)
            or current and evidence.get("petri_inputs") != inputs):
        raise DomainError("stale_evidence", "Publication evidence does not match current Petri inputs", 409)
    return inputs

def freeze_without_publication_policy(task, context):
    """Persist a checked policy during the internal admin-only freeze transaction."""
    from .store import Store
    from .workflow import Place
    from .integration_workflow import satisfactory_validation
    token = Store.workflow_token(task)
    policy = context.get("acceptance_policy")
    _object(policy, ("version", "publication_required", "reason", "requirements", "review_required"))
    _text(policy["version"])
    _text(policy["reason"])
    if (token.place != Place.VALIDATING or context.get("current_inputs") is not True
            or context.get("publication_required") is not False or policy["publication_required"] is not False
            or context.get("publication_policy_version") != token.policy_version
            or policy["version"] != token.policy_version or context.get("policy_reason") != policy["reason"]
            or policy["requirements"] != [stage.value for stage in token.requirements]
            or type(policy["review_required"]) is not bool or not satisfactory_validation(token)):
        raise DomainError("workflow_conflict", "A current explicit no-publication policy is required", 409)
    prepared = deepcopy(task)
    prepared["metadata"]["_skybuild_workflow"]["petri"]["acceptance_policy"] = deepcopy(policy)
    return prepared

def completion_without_publication(task, body, actor, context):
    """Validate internal acceptance under a verified explicit project policy.

    Store calls this only from its admin-only verified producer transaction.
    Public completion_change retains its required publication contract.
    """
    from .store import Store
    from .integration_workflow import satisfactory_validation
    from .workflow import Place, ValidationStage, ResultState, _result_current

    _object(body, ("kind", "reason", "generation", "author", "policy_ref", "acceptance"))
    if body["kind"] != "without_publication":
        raise DomainError("validation", "Unsupported internal completion kind", 422)
    for name in ("reason", "author", "policy_ref"):
        _text(body[name])
    token = Store.workflow_token(task)
    policy = context.get("acceptance_policy")
    _object(policy, ("version", "publication_required", "reason", "requirements", "review_required"))
    _text(policy["version"])
    _text(policy["reason"])
    stored_policy = task["metadata"]["_skybuild_workflow"]["petri"].get("acceptance_policy")
    if stored_policy != policy:
        raise DomainError("workflow_conflict", "Acceptance policy was not frozen with this task", 409)
    if (context.get("publication_required") is not False or policy["publication_required"] is not False
            or policy["version"] != token.policy_version or body["policy_ref"] != policy["version"]
            or context.get("publication_policy_version") != token.policy_version
            or policy["requirements"] != [stage.value for stage in token.requirements]
            or type(policy["review_required"]) is not bool):
        raise DomainError("workflow_conflict", "Current explicit acceptance policy is required", 409)
    if (token.place != Place.INTEGRATING or context.get("current_inputs") is not True
            or token.policy_reason != policy["reason"] or context.get("policy_reason") != policy["reason"]
            or type(body["generation"]) is not int or body["generation"] != generation(task)
            or any(type(context.get(name)) is not type(getattr(token, name))
                   or context.get(name) != getattr(token, name) for name in _ACCEPTANCE_INPUTS)
            or not satisfactory_validation(token)):
        raise DomainError("stale_evidence", "Acceptance inputs or required validation are stale", 409)
    if policy["review_required"]:
        reviews = [result for result in token.evidence
                   if result.stage == ValidationStage.CODE_REVIEW and _result_current(token, result)]
        if (ValidationStage.CODE_REVIEW not in token.requirements or not reviews
                or any(result.state != ResultState.PASSED or result.producer == body["author"]
                       or not result.producer for result in reviews)):
            raise DomainError("workflow_conflict", "Current independent review is required", 409)
    acceptance = body["acceptance"]
    if (not task["acceptance_criteria"] or not isinstance(acceptance, list)
            or len(acceptance) != len(task["acceptance_criteria"])):
        raise DomainError("validation", "Every acceptance criterion requires evidence", 422)
    for criterion, item in zip(task["acceptance_criteria"], acceptance):
        _object(item, ("criterion", "evidence_ref"))
        if item["criterion"] != criterion:
            raise DomainError("stale_evidence", "Acceptance criteria changed", 409)
        _text(item["evidence_ref"])
    evidence = {"schema_version": 1, "actor": actor, "input_revision": task["revision"],
                "definition": definition(task), **deepcopy(body),
                "inputs": {name: getattr(token, name) for name in _ACCEPTANCE_INPUTS},
                "policy": deepcopy(policy)}
    metadata = deepcopy(task["metadata"])
    metadata[RESERVED_KEY] = evidence
    return {"status": "done", "phase": "done", "next_action": None, "blocker": None, "metadata": metadata}


_ACCEPTANCE_INPUTS = ("source_head", "target_base", "definition_revision", "input_generation", "policy_version")


def _current_without_publication(task, evidence):
    from .store import Store
    from .integration_workflow import satisfactory_validation
    from .workflow import Place, ValidationStage, ResultState, _result_current
    token = Store.workflow_token(task)
    if evidence["policy"]["review_required"]:
        reviews = [result for result in token.evidence
                   if result.stage == ValidationStage.CODE_REVIEW and _result_current(token, result)]
        if (ValidationStage.CODE_REVIEW not in token.requirements or not reviews
                or any(result.state != ResultState.PASSED or not result.producer
                       or result.producer == evidence["author"] for result in reviews)):
            return False
    return (token.place == Place.DONE
            and task["metadata"]["_skybuild_workflow"]["petri"].get("acceptance_policy") == evidence["policy"]
            and type(evidence["policy"]["review_required"]) is bool
            and evidence["policy"]["publication_required"] is False
            and evidence["policy"]["version"] == token.policy_version
            and evidence["policy"]["reason"] == token.policy_reason
            and evidence["policy"]["requirements"] == [stage.value for stage in token.requirements]
            and evidence["inputs"] == {name: getattr(token, name) for name in _ACCEPTANCE_INPUTS}
            and satisfactory_validation(token))

def current_completion(task):
    """Historical/imported completion is not current attested acceptance."""
    evidence = task.get("metadata", {}).get(RESERVED_KEY)
    try:
        return (task["status"] == "done" and isinstance(evidence, dict)
                and evidence.get("schema_version") == 1
                and (evidence.get("kind") == "owner_attestation"
                     and (not _has_petri(task) or _publication_token_binding(task, evidence, current=True) is not None)
                     or evidence.get("kind") == "without_publication" and _current_without_publication(task, evidence))
                and evidence.get("definition") == definition(task)
                and evidence.get("generation") == generation(task))
    except (KeyError, TypeError, AttributeError, DomainError):
        return False
