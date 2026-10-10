"""Observe two submitted tasks and freeze their independently reviewed heads.

This planner never creates validation results or review approvals. It reads the
current authority token after external producers finish all five stages.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import re

from .integration_workflow import satisfactory_validation
from .manual_integration import binding, digest, validation_digest
from .store import Store
from .trusted_integration import SCHEMA as INTEGRATION_SCHEMA, verify_signature
from .workflow import Place, ResultState, ValidationStage, _result_current


class ProgressionError(ValueError):
    pass


def reviewed_members(client, project: str, approved: list[dict], *, base_sha: str) -> list[dict] | None:
    """Return exact eligible members, or None while independent work is pending."""
    if (not isinstance(approved, list) or len(approved) != 2
            or len({item.get("task_id") for item in approved}) != 2
            or not isinstance(base_sha, str) or re.fullmatch(r"[0-9a-f]{40}", base_sha) is None):
        raise ProgressionError("Require two distinct approved task identities and one full base")
    members = []
    for expected in approved:
        if set(expected) != {"task_id", "worker", "branch", "reviewer", "review_path"}:
            raise ProgressionError("Approved member fields differ")
        view = client.task_workflow(project, expected["task_id"])
        token = Store.workflow_token(view["task"])
        if (view.get("token") != token.to_dict() or token.project_id != project
                or token.task_id != expected["task_id"]):
            raise ProgressionError("Authority workflow projection differs")
        if token.place == Place.WORKING:
            return None
        if token.place != Place.VALIDATING or token.pending_action is not None or token.superseded:
            raise ProgressionError("Approved task no longer has a valid author submission")
        if (token.responsible != expected["worker"]
                or token.source_branch != "refs/heads/" + expected["branch"]
                or token.target_base != base_sha or not token.source_head
                or re.fullmatch(r"[0-9a-f]{40}", token.source_head) is None):
            raise ProgressionError("Author attempt, branch or frozen base differs")
        if not satisfactory_validation(token):
            return None
        if set(token.requirements) != set(ValidationStage):
            raise ProgressionError("All five configured validation stages are required")
        review_path = Path(expected["review_path"])
        if not review_path.is_absolute() or review_path.is_symlink() or not review_path.is_file():
            raise ProgressionError("Independent review artifact path is invalid")
        raw = review_path.read_bytes()
        if not raw or len(raw) > 262144 or token.source_head.encode() not in raw:
            raise ProgressionError("Independent review artifact does not identify exact head")
        review_sha = hashlib.sha256(raw).hexdigest()
        artifact = str(review_path) + "#sha256=" + review_sha
        reviews = [item for item in token.evidence
                   if item.stage == ValidationStage.CODE_REVIEW and item.state == ResultState.PASSED
                   and _result_current(token, item) and item.producer == expected["reviewer"]
                   and item.producer != token.responsible and artifact in item.artifacts]
        if len(reviews) != 1:
            raise ProgressionError("Current independent review differs from exact artifact")
        members.append({"task_id": token.task_id, "ref": token.source_branch,
                        "head_sha": token.source_head,
                        "review": {"verdict": "pass", "head_sha": token.source_head,
                                   "reviewer": expected["reviewer"],
                                   "evidence": str(review_path)},
                        "workflow": {"project_id": token.project_id,
                                     "attempt_id": token.attempt_id,
                                     "claim_fence": token.claim_fence,
                                     "input_generation": token.input_generation,
                                     "definition_revision": token.definition_revision,
                                     "policy_version": token.policy_version,
                                     "target_base": token.target_base}})
    if len({member["head_sha"] for member in members}) != 2:
        raise ProgressionError("Two task branches must have distinct authored heads")
    return members


def bundle_manifest(members: list[dict], *, target_ref: str, base_sha: str,
                    policy_evidence: Path) -> dict:
    if (len(members) != 2 or not isinstance(target_ref, str)
            or re.fullmatch(r"refs/heads/dev-[0-9]{3}", target_ref) is None
            or not policy_evidence.is_absolute() or not policy_evidence.is_file()):
        raise ProgressionError("Frozen bundle inputs are incomplete")
    return {"schema": "skybuild.bundle-input.v1", "target_ref": target_ref,
            "base_sha": base_sha, "policy_evidence": str(policy_evidence),
            "members": members}


def gate_members(members: list[dict], assignments: list[dict], signed_reviews: list[dict]) -> list[dict]:
    """Map current independent Petri evidence; never create a review verdict."""
    if len(members) != 2 or len(assignments) != 2 or len(signed_reviews) != 2:
        raise ProgressionError("Gate requires two ordered reviewed assignments")
    result = []
    for member, assignment, review in zip(members, assignments, signed_reviews, strict=True):
        if (set(assignment) != {"task_id", "assignment_id", "worker_id", "task_branch",
                                "brief_sha256"}
                or member["task_id"] != assignment["task_id"]
                or member["ref"] != assignment["task_branch"]
                or member["review"]["reviewer"] == assignment["worker_id"]
                or not isinstance(review, dict)
                or set(review) != {"schema", "key_id", "principal", "payload", "signature"}):
            raise ProgressionError("Gate assignment or independent review differs")
        path = Path(member["review"]["evidence"])
        raw = path.read_bytes()
        artifact_sha = hashlib.sha256(raw).hexdigest()
        artifact_uri = str(path) + "#sha256=" + artifact_sha
        workflow = member["workflow"]
        result.append({"task_id": member["task_id"],
                       "assignment_id": assignment["assignment_id"],
                       "worker_id": assignment["worker_id"],
                       "brief_sha256": assignment["brief_sha256"],
                       "head_sha": member["head_sha"],
                       "workflow": {"attempt_id": workflow["attempt_id"],
                                    "claim_fence": workflow["claim_fence"],
                                    "input_generation": workflow["input_generation"],
                                    "definition_revision": workflow["definition_revision"],
                                    "policy_version": workflow["policy_version"],
                                    "source_sha": member["head_sha"],
                                    "base_sha": workflow["target_base"]},
                       "code_review": {"kind": "independent_code_review",
                                       "producer": member["review"]["reviewer"],
                                       "artifact_uri": artifact_uri,
                                       "artifact_sha256": artifact_sha},
                       "review": review,
                       "review_artifact_path": str(path)})
    return result


def frozen_gate_input(client, project: str, members: list[dict], frozen_packets: list[dict],
                      gate_members_value: list[dict], *, policy_sha256: str,
                      target_ref: str, base_sha: str, prepared_manifest_sha256: str,
                      candidate_commit: str, candidate_tree: str,
                      candidate_archive_sha256: str, candidate_history_sha256: str,
                      bundle_id: str, pr_number: int, pr_head_sha: str,
                      pr_base_sha: str, pr_source_ref: str, frozen_at: str,
                      conductor_intent_sha256: str,
                      key: bytes, key_id: str) -> dict:
    """Build unsigned gate input only after both exact freeze receipts are in the API journal."""
    if (len(members) != 2 or len(frozen_packets) != 2 or len(gate_members_value) != 2
            or len({member["task_id"] for member in members}) != 2):
        raise ProgressionError("Frozen gate input needs two distinct tasks")
    for member, packet in zip(members, frozen_packets, strict=True):
        verify_signature(packet, key=key, key_id=key_id)
        view = client.task_workflow(project, member["task_id"])
        token = Store.workflow_token(view["task"])
        packet_bundle = packet.get("bundle")
        if (view.get("token") != token.to_dict() or token.place != Place.INTEGRATING
                or token.pending_action is not None or token.superseded
                or token.source_head != member["head_sha"]
                or token.source_branch != member["ref"] or token.target_base != base_sha
                or set(token.requirements) != set(ValidationStage)
                or not satisfactory_validation(token)
                or packet.get("schema") != INTEGRATION_SCHEMA or packet.get("event") != "freeze"
                or packet.get("binding") != binding(token)
                or packet.get("validation_sha256") != validation_digest(token)
                or not isinstance(packet_bundle, dict)
                or packet_bundle.get("bundle_id") != bundle_id
                or packet_bundle.get("manifest_sha256") != prepared_manifest_sha256
                or packet_bundle.get("target_ref") != target_ref
                or packet_bundle.get("base_commit") != base_sha
                or packet_bundle.get("candidate_commit") != candidate_commit
                or packet_bundle.get("candidate_tree") != candidate_tree):
            raise ProgressionError("Frozen API task differs from signed exact attempt")
        history = client.task_history(project, member["task_id"], limit=100,
                                      offset=max(0, token.revision - 100))
        exact = [row for row in history if row.get("operation") == "workflow.freeze"
                 and row.get("event_facts", {}).get("integration_receipt") == {
                     "authority": "trusted_signed_publisher", "sha256": digest(packet),
                     "evidence": packet}]
        if len(exact) != 1:
            raise ProgressionError("Exact signed freeze is absent from API journal")
    return {"schema": "skybuild.two-task-gate-input.v1", "policy_sha256": policy_sha256,
            "project_id": project, "target_ref": target_ref, "base_sha": base_sha,
            "members": gate_members_value,
            "prepared_manifest_sha256": prepared_manifest_sha256,
            "candidate_commit": candidate_commit, "candidate_tree": candidate_tree,
            "candidate_archive_sha256": candidate_archive_sha256,
            "candidate_history_sha256": candidate_history_sha256,
            "bundle_id": bundle_id, "pr_number": pr_number,
            "pr_head_sha": pr_head_sha, "pr_base_sha": pr_base_sha,
            "pr_source_ref": pr_source_ref,
            "frozen_at": frozen_at, "conductor_intent_sha256": conductor_intent_sha256}
