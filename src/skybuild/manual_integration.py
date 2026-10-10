"""Bounded manual owner attestations, never an automatic publication verifier.

The operator checks external Git/GitHub artifacts before attesting. The server
validates that statement against current database inputs and the original freeze.
Authenticated owner authority, not same-user filesystem isolation, is the trust
boundary. The ordinary automatic integration adapter remains unqualified.
"""
from copy import deepcopy
import hashlib
import json
import re

from .contracts import DomainError, valid_identifier
from .integration_workflow import IntegrationWorkflow

SCHEMA = "skybuild.manual-integration.v1"
DEFAULT_GATE_COMMAND = ["uv", "run", "--extra", "test", "python", "-m", "pytest", "-q"]
DEFAULT_GATE_COMMAND_SHA256 = hashlib.sha256(json.dumps(DEFAULT_GATE_COMMAND).encode()).hexdigest()
BINDING_FIELDS = ("project_id", "task_id", "attempt_id", "claim_fence", "input_generation",
                  "definition_revision", "policy_version", "source_head", "source_branch", "target_base")
FIELDS = {"schema", "event", "operation_id", "expected_revision", "attestor", "authority", "binding",
          "bundle", "validation_sha256", "freeze_sha256", "gate", "publication", "completion"}


def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False,
                          ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise DomainError("validation", "Manual evidence must be finite UTF-8 JSON", 422) from None


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _require(condition, message="Manual integration evidence is invalid", status=422):
    if not condition:
        raise DomainError("stale_evidence" if status == 409 else "validation", message, status)


def _shape(value, fields):
    _require(isinstance(value, dict) and set(value) == set(fields))


def _sha(value, size=40):
    return isinstance(value, str) and re.fullmatch("[0-9a-f]{" + str(size) + "}", value) is not None


def _branch_ref(value):
    return (isinstance(value, str)
            and re.fullmatch(r"refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]{0,180}", value) is not None
            and ".." not in value and "//" not in value
            and all(part and not part.startswith(".") and not part.endswith((".", ".lock"))
                    for part in value.split("/")))


def binding(token):
    return {name: getattr(token, name) for name in BINDING_FIELDS}


def validation_digest(token):
    return digest([item.to_dict() for item in token.evidence])


def validate_evidence(evidence, *, event, operation_id, expected_revision, attestor):
    """Validate the bounded statement, without claiming external verification."""
    _shape(evidence, FIELDS)
    _require(len(canonical(evidence)) <= 49152)
    _require(event in {"freeze", "accept", "integration_progress"})
    _require(evidence["schema"] == SCHEMA and evidence["event"] == event
             and evidence["operation_id"] == operation_id and valid_identifier(operation_id)
             and type(evidence["expected_revision"]) is int
             and evidence["expected_revision"] == expected_revision
             and evidence["attestor"] == attestor
             and evidence["authority"] == "manual_owner_attestation")
    _shape(evidence["binding"], BINDING_FIELDS)
    inputs = evidence["binding"]
    for name in ("project_id", "task_id", "attempt_id", "policy_version"):
        _require(valid_identifier(inputs[name]))
    for name in ("claim_fence", "input_generation", "definition_revision"):
        _require(type(inputs[name]) is int and 0 < inputs[name] < 2**63)
    _require(_sha(inputs["source_head"]) and _sha(inputs["target_base"]))
    _require(_branch_ref(inputs["source_branch"]))
    bundle = evidence["bundle"]
    _shape(bundle, {"bundle_id", "manifest_sha256", "policy_sha256", "target_ref", "base_commit",
                    "candidate_commit", "candidate_tree", "members"})
    _require(valid_identifier(bundle["bundle_id"]) and _sha(bundle["manifest_sha256"], 64)
             and _sha(bundle["policy_sha256"], 64))
    _require(isinstance(bundle["target_ref"], str)
             and re.fullmatch(r"refs/heads/(?:dev-[0-9]{3}|main)", bundle["target_ref"]) is not None)
    _require(all(_sha(bundle[name]) for name in ("base_commit", "candidate_commit", "candidate_tree")))
    _require(bundle["base_commit"] == inputs["target_base"])
    members = bundle["members"]
    _require(isinstance(members, list) and 1 <= len(members) <= 20)
    seen, refs = set(), set()
    for member in members:
        _shape(member, {"task_id", "source_head", "source_branch", "reviewer", "review_sha256"})
        _require(valid_identifier(member["task_id"]) and member["task_id"] not in seen)
        _require(_sha(member["source_head"]) and _sha(member["review_sha256"], 64))
        _require(isinstance(member["reviewer"], str) and 0 < len(member["reviewer"]) <= 200)
        _require(_branch_ref(member["source_branch"])
                 and member["source_branch"] not in refs and member["source_branch"] != bundle["target_ref"])
        seen.add(member["task_id"])
        refs.add(member["source_branch"])
    own = [member for member in members if member["task_id"] == inputs["task_id"]]
    _require(len(own) == 1 and own[0]["source_head"] == inputs["source_head"]
             and own[0]["source_branch"] == inputs["source_branch"])
    _require(_sha(evidence["validation_sha256"], 64))
    if event == "freeze":
        _require(all(evidence[name] is None for name in ("freeze_sha256", "gate", "publication", "completion")))
        return
    _require(_sha(evidence["freeze_sha256"], 64))
    if event == "integration_progress":
        _require(evidence["gate"] is None and evidence["completion"] is None)
        _shape(evidence["publication"], {"outcome", "operation_ref"})
        _require(evidence["publication"]["outcome"] == "unknown"
                 and valid_identifier(evidence["publication"]["operation_ref"]))
        return
    gate = evidence["gate"]
    _shape(gate, {"run_id", "head", "tree", "command_sha256", "artifact_sha256", "phase", "status", "cleanup", "exit_code"})
    _require(valid_identifier(gate["run_id"]) and _sha(gate["head"]) and _sha(gate["tree"])
             and _sha(gate["artifact_sha256"], 64) and gate["command_sha256"] == DEFAULT_GATE_COMMAND_SHA256
             and gate["phase"] == "terminal" and gate["status"] == "passed"
             and gate["cleanup"] == "confirmed" and type(gate["exit_code"]) is int and gate["exit_code"] == 0
             and gate["tree"] == bundle["candidate_tree"])
    publication = evidence["publication"]
    _shape(publication, {"repository", "pr", "head_commit", "base_ref", "base_commit", "target_commit",
                         "target_tree", "observed_target_commit", "task_head", "observation_sha256"})
    _require(publication["repository"] == "stonesky-ai/skybuild" and type(publication["pr"]) is int
             and 0 < publication["pr"] < 2**31
             and all(_sha(publication[name]) for name in ("head_commit", "base_commit", "target_commit",
                                                           "target_tree", "observed_target_commit", "task_head"))
             and _sha(publication["observation_sha256"], 64))
    _require(publication["head_commit"] == bundle["candidate_commit"]
             and publication["base_ref"] == bundle["target_ref"]
             and publication["base_commit"] == bundle["base_commit"]
             and publication["target_tree"] == bundle["candidate_tree"]
             and publication["task_head"] == inputs["source_head"])
    _require(isinstance(evidence["completion"], dict))
    completion = evidence["completion"]
    _require(completion.get("source_head") == inputs["source_head"]
             and completion.get("policy_ref") == inputs["policy_version"])
    review = completion.get("review", {})
    review_ref = "manual-review:" + own[0]["review_sha256"]
    _require(isinstance(review, dict) and review.get("reviewer") == own[0]["reviewer"]
             and review.get("session_ref") == review_ref and review.get("evidence_ref") == review_ref)
    _require(completion.get("publication") == {
        "source_head": inputs["source_head"], "candidate_commit": gate["head"],
        "base_commit": bundle["base_commit"], "target_commit": publication["target_commit"],
        "target_ref": bundle["target_ref"], "result": "confirmed",
        "inclusion_evidence_ref": "manual-integration:" + publication["observation_sha256"]})


def transition(store, principal, project_id, task_id, event, evidence, expected_revision, operation_id):
    """Attest manually under Store authorization, CAS and current-input guards."""
    if not principal.is_admin:
        raise DomainError("authorization", "Only owner/admin may attest manual integration", 403)
    validate_evidence(evidence, event=event, operation_id=operation_id,
                      expected_revision=expected_revision, attestor=principal.principal_id)
    packet = deepcopy(evidence)

    def verify(connection, task, context, supplied):
        from .store import Store
        token = Store.workflow_token(task)
        _require(supplied == packet and packet["binding"] == binding(token)
                 and token.project_id == project_id and token.task_id == task_id,
                 "Manual evidence differs from current task inputs", 409)
        _require(packet["validation_sha256"] == validation_digest(token),
                 "Manual validation snapshot is stale", 409)
        bundle = packet["bundle"]
        if event != "freeze":
            row = connection.execute(
                "SELECT event_facts FROM task_journal WHERE project_id = %s AND task_id = %s "
                "AND operation = %s ORDER BY revision DESC LIMIT 1",
                (project_id, task_id, "workflow.freeze")).fetchone()
            saved = row["event_facts"].get("integration_receipt", {}) if row else {}
            frozen = saved.get("evidence", {})
            _require(saved.get("sha256") == packet["freeze_sha256"] and frozen.get("event") == "freeze"
                     and frozen.get("bundle") == bundle and frozen.get("binding") == packet["binding"]
                     and frozen.get("validation_sha256") == packet["validation_sha256"]
                     and token.bundle_id == bundle["bundle_id"], "Manual evidence differs from the frozen bundle", 409)
        facts = {"publication_required": True, "bundle_id": bundle["bundle_id"]}
        if event == "freeze":
            facts["integration_fixed"] = True
        elif event == "accept":
            _require(packet["completion"].get("author") == token.responsible,
                     "Manual acceptance author differs from the submitted attempt", 409)
            facts.update(acceptance_verified=True, publication_verified=True, task_included=True,
                         completion_evidence=packet["completion"])
        else:
            facts.update(integration_observation_verified=True, publication_outcome="unknown")
        return facts

    receipt = {"authority": "manual_owner_attestation", "sha256": digest(packet), "evidence": packet}
    # The same bounded envelope is journaled and included in the idempotency input.
    def envelope_verifier(connection, task, context, supplied):
        _require(supplied == receipt)
        return verify(connection, task, context, supplied["evidence"])

    adapter = IntegrationWorkflow(store, envelope_verifier)
    return adapter.transition(principal, project_id, task_id, event, evidence=receipt,
                              expected_revision=expected_revision, idempotency_key=operation_id,
                              **({"reason": "Owner attests unresolved publication; retain integration ownership"}
                                 if event == "integration_progress" else {}))
