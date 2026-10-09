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
    evidence = {"schema_version": 1, "kind": "owner_attestation", "actor": actor,
                "input_revision": task["revision"], "definition": definition(task), **deepcopy(body)}
    metadata = deepcopy(task["metadata"])
    metadata[RESERVED_KEY] = evidence
    return {"status": "done", "phase": "done", "next_action": None, "blocker": None, "metadata": metadata}


def current_completion(task):
    """Historical/imported completion is not current attested acceptance."""
    evidence = task.get("metadata", {}).get(RESERVED_KEY)
    try:
        return (task["status"] == "done" and isinstance(evidence, dict)
                and evidence.get("schema_version") == 1 and evidence.get("kind") == "owner_attestation"
                and evidence.get("definition") == definition(task)
                and evidence.get("generation") == generation(task))
    except (KeyError, DomainError):
        return False
