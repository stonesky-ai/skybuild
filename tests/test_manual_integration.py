"""Strict manual owner attestations bind a frozen bundle to checked publication."""
from copy import deepcopy
from dataclasses import replace
import hashlib

import pytest

from skybuild.completion import generation
from skybuild.contracts import DomainError
from skybuild.manual_integration import (
    DEFAULT_GATE_COMMAND_SHA256, SCHEMA, binding, canonical, digest,
    validate_evidence, validation_digest,
)
from skybuild.workflow import Place, ResultState, TaskToken, ValidationResult, ValidationStage


def _principal(owner):
    return getattr(owner, "principal_id", owner)


def _token(value):
    if isinstance(value, TaskToken):
        return value
    if "token" in value:
        return TaskToken.from_dict(value["token"])
    from skybuild.store import Store
    return Store.workflow_token(value)


def freeze_evidence(token, owner, operation_id="freeze"):
    """Create a complete strict packet for unit and API transition tests."""
    token = _token(token)
    return {
        "schema": SCHEMA, "event": "freeze", "operation_id": operation_id,
        "expected_revision": token.revision, "attestor": _principal(owner),
        "authority": "manual_owner_attestation", "binding": binding(token),
        "bundle": {"bundle_id": "manual-bundle", "manifest_sha256": "1" * 64,
            "policy_sha256": "2" * 64, "target_ref": "refs/heads/dev-004",
            "base_commit": token.target_base, "candidate_commit": "c" * 40,
            "candidate_tree": "d" * 40,
            "members": [{"task_id": token.task_id, "source_head": token.source_head,
                "source_branch": token.source_branch, "reviewer": "independent-reviewer",
                "review_sha256": "3" * 64}]},
        "validation_sha256": validation_digest(token), "freeze_sha256": None,
        "gate": None, "publication": None, "completion": None,
    }


def accept_evidence(view, frozen, owner, operation_id="accept"):
    """Attest a passed default gate and complete exact-task publication."""
    token = _token(view)
    task = view.get("task", view)
    packet = deepcopy(frozen)
    packet.update(event="accept", operation_id=operation_id,
                  expected_revision=token.revision, attestor=_principal(owner),
                  freeze_sha256=digest(frozen))
    bundle = packet["bundle"]
    member = next(item for item in bundle["members"] if item["task_id"] == token.task_id)
    review_ref = "manual-review:" + member["review_sha256"]
    packet["gate"] = {"run_id": "gate-1", "head": "e" * 40,
        "tree": bundle["candidate_tree"], "command_sha256": DEFAULT_GATE_COMMAND_SHA256,
        "artifact_sha256": "4" * 64, "phase": "terminal", "status": "passed",
        "cleanup": "confirmed", "exit_code": 0}
    packet["publication"] = {"repository": "stonesky-ai/skybuild", "pr": 71,
        "head_commit": bundle["candidate_commit"], "base_ref": bundle["target_ref"],
        "base_commit": bundle["base_commit"], "target_commit": "f" * 40,
        "target_tree": bundle["candidate_tree"], "observed_target_commit": "f" * 40,
        "task_head": token.source_head, "observation_sha256": "5" * 64}
    packet["completion"] = {
        "reason": "Owner checked the gate and exact published task inclusion",
        "generation": generation(task), "source_head": token.source_head,
        "author": token.responsible, "policy_ref": token.policy_version,
        "acceptance": [{"criterion": item, "evidence_ref": "manual-smoke/acceptance"}
                       for item in task["acceptance_criteria"]],
        "checks": [{"name": "default disposable PostgreSQL gate", "source_head": token.source_head,
                    "result": "passed", "evidence_ref": "manual-smoke/gate"}],
        "review": {"reviewer": member["reviewer"], "session_ref": review_ref,
            "source_head": token.source_head, "result": "passed", "unresolved_blocking_findings": 0,
            "evidence_ref": review_ref},
        "publication": {"source_head": token.source_head, "candidate_commit": packet["gate"]["head"],
            "base_commit": bundle["base_commit"], "target_commit": packet["publication"]["target_commit"],
            "target_ref": bundle["target_ref"], "result": "confirmed",
            "inclusion_evidence_ref": "manual-integration:" + packet["publication"]["observation_sha256"]},
    }
    return packet


@pytest.fixture
def token():
    values = dict(source_head="a" * 40, target_base="b" * 40, attempt_id="attempt-1",
                  claim_fence=1, input_generation=2, definition_revision=1, policy_version="policy-1")
    evidence = tuple(ValidationResult("project", "task", stage, ResultState.PASSED,
        producer="independent-reviewer" if stage == ValidationStage.CODE_REVIEW else "worker",
        check_id=stage.value, **values) for stage in ValidationStage)
    return TaskToken("project", "task", place=Place.VALIDATING,
        source_branch="refs/heads/task/smoke", revision=9,
        requirements=tuple(ValidationStage), evidence=evidence, **values)


@pytest.fixture
def frozen(token):
    return freeze_evidence(token, "owner")


@pytest.fixture
def accepted(token, frozen):
    current = replace(token, place=Place.INTEGRATING, bundle_id="manual-bundle", revision=10)
    return accept_evidence({"token": current.to_dict(), "task": {
        "metadata": {"_skybuild_workflow": {"generation": 3}},
        "acceptance_criteria": ["Verify the README line"]}}, frozen, "owner")


def validate(packet):
    return validate_evidence(packet, event=packet["event"], operation_id=packet["operation_id"],
        expected_revision=packet["expected_revision"], attestor="owner")


def test_packets_and_canonical_digest_are_stable(token, frozen, accepted):
    validate(frozen)
    validate(accepted)
    assert digest({"b": 2, "a": "é"}) == hashlib.sha256('{"a":"é","b":2}'.encode()).hexdigest()
    assert binding(token) == frozen["binding"]
    assert validation_digest(token) != validation_digest(replace(token, evidence=()))
    assert accepted["freeze_sha256"] == digest(frozen)


@pytest.mark.parametrize("field,value", [
    ("schema", "unknown"), ("event", "integration_failure"), ("operation_id", "bad\nkey"),
    ("expected_revision", True), ("attestor", "worker"), ("authority", "automatic"),
    ("validation_sha256", "short"), ("guard_facts", {"publication_verified": True}),
    ("validation_verified", True), ("publication_verified", True),
])
def test_freeze_rejects_unsupported_or_untrusted_envelope(frozen, field, value):
    frozen[field] = value
    with pytest.raises(DomainError):
        validate(frozen)


@pytest.mark.parametrize("argument,value", [
    ("event", "accept"), ("operation_id", "another"), ("expected_revision", 8), ("attestor", "another"),
])
def test_envelope_must_match_request(frozen, argument, value):
    arguments = dict(event="freeze", operation_id="freeze", expected_revision=9, attestor="owner")
    arguments[argument] = value
    with pytest.raises(DomainError):
        validate_evidence(frozen, **arguments)


@pytest.mark.parametrize("path,field", [
    ((), "binding"), (("binding",), "attempt_id"), (("bundle",), "policy_sha256"),
    (("bundle", "members", 0), "review_sha256"),
])
def test_required_envelope_fields_cannot_be_omitted(frozen, path, field):
    value = frozen
    for component in path:
        value = value[component]
    value.pop(field)
    with pytest.raises(DomainError):
        validate(frozen)


@pytest.mark.parametrize("field,value", [
    ("claim_fence", True), ("input_generation", 0), ("definition_revision", -1),
    ("input_generation", 2**63), ("source_head", "A" * 40), ("target_base", "b" * 39),
    ("attempt_id", None), ("policy_version", "bad\npolicy"), ("unexpected", True),
    ("source_branch", "main"), ("source_branch", "refs/heads/a..b"),
    ("source_branch", "refs/heads/a.lock"), ("source_branch", "refs/heads/a//b"),
])
def test_binding_rejects_invalid_counters_ids_heads_and_refs(frozen, field, value):
    frozen["binding"][field] = value
    with pytest.raises(DomainError):
        validate(frozen)


@pytest.mark.parametrize("change", [
    lambda p: p["bundle"].update(members=[]),
    lambda p: p["bundle"]["members"].append(deepcopy(p["bundle"]["members"][0])),
    lambda p: p["bundle"]["members"][0].update(task_id="another"),
    lambda p: p["bundle"]["members"][0].update(source_head="0" * 40),
    lambda p: p["bundle"]["members"][0].update(source_branch="refs/heads/task/another"),
    lambda p: p["bundle"]["members"][0].update(reviewer=""),
    lambda p: p["bundle"].update(base_commit="0" * 40),
    lambda p: p["bundle"].update(target_ref="refs/heads/dev-4"),
    lambda p: p["bundle"].update(candidate_tree="short"),
    lambda p: p["bundle"].update(extra=True),
])
def test_bundle_rejects_missing_duplicate_or_mismatched_members(frozen, change):
    change(frozen)
    with pytest.raises(DomainError):
        validate(frozen)


@pytest.mark.parametrize("branch", ["refs/heads/.hidden", "refs/heads/end.", "refs/heads/a/",
                                    "refs/heads/a@{b", "refs/heads/a\\b"])
def test_every_member_requires_a_valid_distinct_git_branch(frozen, branch):
    frozen["bundle"]["members"].append({"task_id": "another", "source_head": "0" * 40,
        "source_branch": branch, "reviewer": "reviewer", "review_sha256": "6" * 64})
    with pytest.raises(DomainError):
        validate(frozen)


def test_distinct_tasks_cannot_share_one_source_branch(frozen):
    member = deepcopy(frozen["bundle"]["members"][0])
    member["task_id"] = "another"
    frozen["bundle"]["members"].append(member)
    with pytest.raises(DomainError):
        validate(frozen)


@pytest.mark.parametrize("field", ["freeze_sha256", "gate", "publication", "completion"])
def test_freeze_cannot_smuggle_acceptance(frozen, field):
    frozen[field] = {}
    with pytest.raises(DomainError):
        validate(frozen)


@pytest.mark.parametrize("field,value", [
    ("command_sha256", "0" * 64), ("tree", "0" * 40), ("status", "failed"),
    ("phase", "running"), ("cleanup", "pending"), ("exit_code", False),
    ("exit_code", 1), ("artifact_sha256", "short"), ("extra", True),
])
def test_accept_requires_default_passed_terminal_gate_and_cleanup(accepted, field, value):
    accepted["gate"][field] = value
    with pytest.raises(DomainError):
        validate(accepted)


@pytest.mark.parametrize("field,value", [
    ("repository", "stonesky-ai/skykeep"), ("pr", True), ("pr", 0),
    ("head_commit", "0" * 40), ("base_ref", "refs/heads/main"),
    ("base_commit", "0" * 40), ("target_tree", "0" * 40), ("task_head", "0" * 40),
    ("target_commit", "short"), ("observation_sha256", "short"), ("extra", True),
])
def test_accept_rejects_wrong_publication_identity(accepted, field, value):
    accepted["publication"][field] = value
    with pytest.raises(DomainError):
        validate(accepted)


@pytest.mark.parametrize("field,value", [
    ("source_head", "0" * 40), ("policy_ref", "old-policy"),
    ("publication", {}),
])
def test_accept_completion_must_bind_exact_publication(accepted, field, value):
    accepted["completion"][field] = value
    with pytest.raises(DomainError):
        validate(accepted)


@pytest.mark.parametrize("field", ["candidate_commit", "base_commit", "target_commit", "target_ref",
                                   "source_head", "result", "inclusion_evidence_ref"])
def test_completion_cannot_attest_a_different_publication(accepted, field):
    accepted["completion"]["publication"][field] = "unrelated"
    with pytest.raises(DomainError):
        validate(accepted)


@pytest.mark.parametrize("field,value", [
    ("reviewer", "unrelated-reviewer"), ("session_ref", "unrelated-session"),
    ("evidence_ref", "unrelated-evidence"),
])
def test_completion_review_must_match_frozen_review(accepted, field, value):
    accepted["completion"]["review"][field] = value
    with pytest.raises(DomainError):
        validate(accepted)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), {1, 2}, "\ud800"])
def test_canonical_rejects_nonfinite_or_non_json_values(value):
    with pytest.raises(DomainError):
        canonical(value)


def test_evidence_is_bounded(frozen):
    frozen["bundle"]["members"][0]["reviewer"] = "x" * 50000
    with pytest.raises(DomainError):
        validate(frozen)


def test_unknown_publication_is_only_progress_and_retains_no_completion(frozen):
    progress = deepcopy(frozen)
    progress.update(event="integration_progress", freeze_sha256=digest(frozen),
                    publication={"outcome": "unknown", "operation_ref": "publication-1"})
    validate(progress)
    progress["publication"]["outcome"] = "confirmed"
    with pytest.raises(DomainError):
        validate(progress)


@pytest.mark.parametrize("field", ["gate", "completion"])
def test_unresolved_progress_cannot_carry_success_evidence(frozen, field):
    progress = deepcopy(frozen)
    progress.update(event="integration_progress", freeze_sha256=digest(frozen),
                    publication={"outcome": "unknown", "operation_ref": "publication-1"})
    progress[field] = {"passed": True}
    with pytest.raises(DomainError):
        validate(progress)
