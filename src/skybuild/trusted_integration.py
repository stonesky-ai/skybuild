"""Signed, source-bound integration statements from one trusted host producer.

The signing key belongs to the operator and the controller, never a task worker
or candidate test container. A signature authenticates a producer statement;
the producer must independently verify the external Git, gate and publisher
evidence before signing. The database transaction still checks current inputs.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat

from .contracts import DomainError, valid_identifier
from .integration_workflow import IntegrationWorkflow
from .manual_integration import binding, canonical, digest, validation_digest
from .store import Store
from .workflow import Place, ResultState, ValidationStage, _result_current


SCHEMA = "skybuild.trusted-integration.v1"
DOMAIN = b"skybuild/trusted-integration/v1\x00"
FIELDS = {"schema", "event", "operation_id", "expected_revision", "producer", "key_id",
          "binding", "validation_sha256", "bundle", "freeze_sha256", "gate",
          "publication", "completion", "signature"}
_SHA = re.compile(r"[0-9a-f]{40}")
_DIGEST = re.compile(r"[0-9a-f]{64}")


def _deny(message: str, status: int = 409) -> None:
    raise DomainError("stale_evidence" if status == 409 else "validation", message, status)


def _key(path: Path) -> bytes:
    if not path.is_absolute() or path.resolve() != path:
        _deny("Trusted integration key path is not canonical")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or not 32 <= info.st_size <= 4096):
            _deny("Trusted integration key is not private")
        return os.read(descriptor, 4097)
    finally:
        os.close(descriptor)


def _unsigned(packet: dict) -> bytes:
    if not isinstance(packet, dict) or set(packet) != FIELDS:
        _deny("Trusted integration statement fields differ", 422)
    value = {name: item for name, item in packet.items() if name != "signature"}
    raw = canonical(value)
    if len(raw) > 49152:
        _deny("Trusted integration statement exceeds its bound", 422)
    return DOMAIN + raw


def sign(packet: dict, *, key: bytes) -> dict:
    """Sign an already verified producer statement; never infer verification."""
    if len(key) < 32 or len(key) > 4096 or packet.get("signature") is not None:
        _deny("Trusted integration signing input is invalid", 422)
    signed = dict(packet)
    signed["signature"] = hmac.new(key, _unsigned(signed), hashlib.sha256).hexdigest()
    return signed


def verify_signature(packet: dict, *, key: bytes, key_id: str) -> None:
    raw = _unsigned(packet)
    signature = packet.get("signature")
    if (packet.get("schema") != SCHEMA or packet.get("key_id") != key_id
            or not isinstance(signature, str) or not _DIGEST.fullmatch(signature)
            or not hmac.compare_digest(signature, hmac.new(key, raw, hashlib.sha256).hexdigest())):
        _deny("Trusted integration signature is invalid")


def _member_review(token, member: dict) -> None:
    fields = {"task_id", "source_head", "source_branch", "reviewer", "review_sha256", "review_artifact"}
    if (not isinstance(member, dict) or set(member) != fields
            or member["task_id"] != token.task_id or member["source_head"] != token.source_head
            or member["source_branch"] != token.source_branch
            or not isinstance(member["review_sha256"], str)
            or not _DIGEST.fullmatch(member["review_sha256"])
            or not isinstance(member["review_artifact"], str)
            or not member["review_artifact"].endswith("#sha256=" + member["review_sha256"])
            or not valid_identifier(member["reviewer"])
            or member["reviewer"] == token.responsible):
        _deny("Frozen member lacks independent exact-head review")
    matched = [result for result in token.evidence
               if result.stage == ValidationStage.CODE_REVIEW and result.state == ResultState.PASSED
               and _result_current(token, result) and result.producer == member["reviewer"]
               and member["review_artifact"] in result.artifacts]
    if len(matched) != 1:
        _deny("Current independent review does not match frozen evidence")


def _all_required_passed(token) -> bool:
    """Only this signed two-worker route requires actual PASS at all five stages."""
    if set(token.requirements) != set(ValidationStage):
        return False
    for stage in ValidationStage:
        current = [item for item in token.evidence
                   if item.stage == stage and _result_current(token, item)]
        if len(current) != 1 or current[0].state != ResultState.PASSED:
            return False
    return True


def _checked_bundle(packet: dict, token) -> dict:
    bundle = packet.get("bundle")
    fields = {"bundle_id", "manifest_sha256", "policy_sha256", "target_ref",
              "base_commit", "candidate_commit", "candidate_tree", "members"}
    if not isinstance(bundle, dict) or set(bundle) != fields:
        _deny("Frozen bundle statement differs", 422)
    if (not isinstance(bundle["manifest_sha256"], str)
            or not _DIGEST.fullmatch(bundle["manifest_sha256"])
            or bundle["bundle_id"] != "bundle-" + bundle["manifest_sha256"][:24]
            or not isinstance(bundle["policy_sha256"], str)
            or not _DIGEST.fullmatch(bundle["policy_sha256"])
            or not isinstance(bundle["target_ref"], str)
            or re.fullmatch(r"refs/heads/(?:dev-[0-9]{3}|main)", bundle["target_ref"]) is None
            or any(not isinstance(bundle[name], str) or not _SHA.fullmatch(bundle[name])
                   for name in ("base_commit", "candidate_commit", "candidate_tree"))
            or bundle["base_commit"] != token.target_base):
        _deny("Frozen bundle identity or target differs")
    members = bundle["members"]
    if (not isinstance(members, list) or not 1 <= len(members) <= 20
            or len({member.get("task_id") for member in members if isinstance(member, dict)}) != len(members)):
        _deny("Frozen bundle members are invalid")
    member_fields = {"task_id", "source_head", "source_branch", "reviewer", "review_sha256", "review_artifact"}
    for member in members:
        if (not isinstance(member, dict) or set(member) != member_fields
                or not valid_identifier(member["task_id"])
                or not isinstance(member["source_head"], str) or not _SHA.fullmatch(member["source_head"])
                or not isinstance(member["source_branch"], str)
                or re.fullmatch(r"refs/heads/task/[A-Za-z0-9._/-]+", member["source_branch"]) is None
                or ".." in member["source_branch"] or "//" in member["source_branch"]
                or not isinstance(member["review_sha256"], str)
                or not _DIGEST.fullmatch(member["review_sha256"])
                or not isinstance(member["review_artifact"], str)
                or not member["review_artifact"].endswith("#sha256=" + member["review_sha256"])):
            _deny("Frozen bundle member shape is invalid")
    own = [member for member in members if member.get("task_id") == token.task_id]
    if len(own) != 1:
        _deny("Task is absent from frozen bundle")
    _member_review(token, own[0])
    return bundle


def _accept_artifacts(packet: dict, bundle: dict) -> dict:
    gate, publication = packet.get("gate"), packet.get("publication")
    if (not isinstance(gate, dict) or set(gate) != {"attestation_sha256", "predicate"}
            or not isinstance(gate["attestation_sha256"], str)
            or not _DIGEST.fullmatch(gate["attestation_sha256"])
            or not isinstance(gate["predicate"], dict)
            or gate["predicate"].get("candidate_commit") != bundle["candidate_commit"]
            or gate["predicate"].get("candidate_tree") != bundle["candidate_tree"]
            or gate["predicate"].get("target_base") != bundle["base_commit"]
            or not isinstance(publication, dict)
            or set(publication) != {"intent_sha256", "state", "candidate", "tree", "expected_base",
                                    "target_ref", "observed_target", "pr", "bundle_id"}
            or publication["state"] != "confirmed"
            or publication["candidate"] != bundle["candidate_commit"]
            or publication["tree"] != bundle["candidate_tree"]
            or publication["expected_base"] != bundle["base_commit"]
            or publication["target_ref"] != bundle["target_ref"]
            or publication["bundle_id"] != bundle["bundle_id"]
            or not isinstance(publication["observed_target"], str)
            or not _SHA.fullmatch(publication["observed_target"])
            or type(publication["pr"]) is not int or publication["pr"] <= 0
            or not isinstance(publication["intent_sha256"], str)
            or not _DIGEST.fullmatch(publication["intent_sha256"])):
        _deny("Signed gate or CAS publication statement differs")
    completion = packet.get("completion")
    if not isinstance(completion, dict):
        _deny("Signed acceptance completion is missing")
    return completion


def _checked_packet(packet: dict, *, event: str, operation_id: str, revision: int,
                    producer: str, key_id: str, key: bytes, token) -> dict:
    verify_signature(packet, key=key, key_id=key_id)
    if (packet["event"] != event or event not in {"freeze", "accept"}
            or packet["operation_id"] != operation_id or not valid_identifier(operation_id)
            or type(packet["expected_revision"]) is not int or packet["expected_revision"] != revision
            or packet["producer"] != producer or packet["binding"] != binding(token)
            or packet["validation_sha256"] != validation_digest(token)
            or set(token.requirements) != set(ValidationStage)
            or not _all_required_passed(token) or token.pending_action is not None
            or token.superseded):
        _deny("Trusted integration statement differs from current inputs")
    bundle = _checked_bundle(packet, token)
    if event == "freeze":
        if (token.place != Place.VALIDATING
                or any(packet[name] is not None for name in ("freeze_sha256", "gate", "publication", "completion"))):
            _deny("Freeze statement has wrong place or later-stage evidence")
    else:
        if (token.place != Place.INTEGRATING or token.bundle_id != bundle["bundle_id"]
                or not isinstance(packet["freeze_sha256"], str)
                or not _DIGEST.fullmatch(packet["freeze_sha256"])):
            _deny("Acceptance lacks its original frozen task binding")
        _accept_artifacts(packet, bundle)
    return bundle


def transition(store, principal, project_id: str, task_id: str, event: str,
               packet: dict, revision: int, operation_id: str, *,
               producer_id: str, key_id: str, key_path: Path):
    """Verify one signed producer statement inside the guarded task transaction."""
    if (principal.principal_id != producer_id or principal.is_admin
            or "integration:attest" not in principal.grants.get(project_id, ())):
        raise DomainError("authorization", "Trusted integration producer identity differs", 403)
    key = _key(key_path)

    def verify(connection, before, context, evidence):
        token = Store.workflow_token(before)
        bundle = _checked_packet(evidence, event=event, operation_id=operation_id,
                                 revision=revision, producer=producer_id,
                                 key_id=key_id, key=key, token=token)
        if token.project_id != project_id or token.task_id != task_id:
            _deny("Trusted integration project or task differs")
        if event == "freeze":
            if context.get("dependencies_satisfied") is not True:
                _deny("Current dependencies are required for freeze")
            return {"validation_verified": True, "integration_fixed": True,
                    "publication_required": True, "bundle_id": bundle["bundle_id"]}
        row = connection.execute(
            "SELECT event_facts FROM task_journal WHERE project_id = %s AND task_id = %s "
            "AND operation = %s ORDER BY revision DESC LIMIT 1",
            (project_id, task_id, "workflow.freeze")).fetchone()
        saved = row["event_facts"].get("integration_receipt") if row else None
        frozen = saved.get("evidence") if isinstance(saved, dict) else None
        if (not isinstance(frozen, dict) or frozen.get("schema") != SCHEMA
                or saved.get("sha256") != packet["freeze_sha256"]
                or digest(frozen) != packet["freeze_sha256"]
                or frozen.get("binding") != packet["binding"]
                or frozen.get("bundle") != bundle
                or frozen.get("validation_sha256") != packet["validation_sha256"]):
            _deny("Acceptance differs from original signed freeze")
        verify_signature(frozen, key=key, key_id=key_id)
        completion = _accept_artifacts(packet, bundle)
        return {"publication_required": True, "acceptance_verified": True,
                "publication_verified": True, "task_included": True,
                "completion_kind": "trusted_publisher", "completion_evidence": completion}

    receipt = {"authority": "trusted_signed_publisher", "sha256": digest(packet), "evidence": packet}
    adapter = IntegrationWorkflow(store, verify_receipt=lambda conn, before, context, supplied:
                                  verify(conn, before, context, supplied["evidence"])
                                  if supplied == receipt else _deny("Trusted receipt was changed"),
                                  trusted_operation="integration:attest")
    return adapter.transition(principal, project_id, task_id, event, evidence=receipt,
                              expected_revision=revision, idempotency_key=operation_id)
