"""Signed producer statements stay bound to current independent review and CAS inputs."""

from dataclasses import replace

import pytest

from skybuild.contracts import DomainError
from skybuild.manual_integration import binding, validation_digest
from skybuild.trusted_integration import SCHEMA, _checked_packet, sign, verify_signature
from skybuild.workflow import Place, ResultState, TaskToken, ValidationResult, ValidationStage


KEY = b"trusted-integration-fixture-key-32-bytes"
REVIEW = "/private/review.txt#sha256=" + "3" * 64


def token():
    values = dict(source_head="a" * 40, target_base="b" * 40, attempt_id="attempt-1",
                  claim_fence=2, input_generation=3, definition_revision=4,
                  policy_version="petri-checks-v1")
    results = tuple(ValidationResult("project", "task", stage, ResultState.PASSED,
                    producer="reviewer" if stage == ValidationStage.CODE_REVIEW else "checker",
                    check_id=stage.value,
                    artifacts=(REVIEW,) if stage == ValidationStage.CODE_REVIEW else (),
                    **values) for stage in ValidationStage)
    return TaskToken("project", "task", place=Place.VALIDATING,
                     source_branch="refs/heads/task/patch", revision=9,
                     requirements=tuple(ValidationStage), evidence=results, **values)


def packet(current, event="freeze"):
    bundle = {"bundle_id": "bundle-" + "1" * 24,
              "manifest_sha256": "1" * 64, "policy_sha256": "2" * 64,
              "target_ref": "refs/heads/dev-006", "base_commit": current.target_base,
              "candidate_commit": "c" * 40, "candidate_tree": "d" * 40,
              "members": [{"task_id": current.task_id, "source_head": current.source_head,
                           "source_branch": current.source_branch, "reviewer": "reviewer",
                           "review_sha256": "3" * 64, "review_artifact": REVIEW}]}
    value = {"schema": SCHEMA, "event": event, "operation_id": event + "-1",
             "expected_revision": current.revision, "producer": "trusted-producer",
             "key_id": "key-1", "binding": binding(current),
             "validation_sha256": validation_digest(current), "bundle": bundle,
             "freeze_sha256": None, "gate": None, "publication": None,
             "completion": None, "signature": None}
    return sign(value, key=KEY)


def check(value, current):
    return _checked_packet(value, event=value["event"], operation_id=value["operation_id"],
                           revision=value["expected_revision"], producer="trusted-producer",
                           key_id="key-1", key=KEY, token=current)


def test_signed_freeze_binds_current_review_and_full_tuple():
    current = token()
    assert check(packet(current), current)["candidate_commit"] == "c" * 40
    changed = replace(current, claim_fence=3)
    with pytest.raises(DomainError):
        check(packet(current), changed)
    reduced = replace(current, requirements=(ValidationStage.CODE_REVIEW,))
    with pytest.raises(DomainError):
        check(packet(reduced), reduced)


def test_signed_two_worker_route_rejects_policy_reason_not_applicable():
    current = token()
    changed = replace(current, evidence=tuple(
        replace(result, state=ResultState.NOT_APPLICABLE,
                policy_reason="Permitted for a different task policy")
        if result.stage == ValidationStage.LONG_TESTS else result
        for result in current.evidence))
    with pytest.raises(DomainError):
        check(packet(changed), changed)


def test_signature_rejects_mutation_wrong_key_and_wrong_domain():
    current = token()
    frozen = packet(current)
    with pytest.raises(DomainError):
        verify_signature(frozen, key=b"x" * 32, key_id="key-1")
    changed = {**frozen, "expected_revision": 10}
    with pytest.raises(DomainError):
        check(changed, current)
    with pytest.raises(DomainError):
        verify_signature(frozen, key=KEY, key_id="other")


def test_review_must_be_current_independent_and_exact_artifact():
    current = token()
    self_review = replace(current, responsible="reviewer")
    with pytest.raises(DomainError):
        check(packet(current), self_review)
    missing = replace(current, evidence=tuple(result for result in current.evidence
                                               if result.stage != ValidationStage.CODE_REVIEW))
    with pytest.raises(DomainError):
        check(packet(missing), missing)
    altered = packet(current)
    changed = {**altered, "bundle": {**altered["bundle"], "members": [
        {**altered["bundle"]["members"][0], "review_artifact": "other#sha256=" + "3" * 64}]}}
    changed["signature"] = None
    with pytest.raises(DomainError):
        check(sign(changed, key=KEY), current)


def test_accept_requires_frozen_binding_and_confirmed_publication_shape():
    current = replace(token(), place=Place.INTEGRATING,
                      bundle_id="bundle-" + "1" * 24, revision=10)
    value = packet(current, "accept")
    value["signature"] = None
    value["freeze_sha256"] = "4" * 64
    value["gate"] = {"attestation_sha256": "5" * 64,
                     "predicate": {"candidate_commit": "c" * 40,
                                   "candidate_tree": "d" * 40,
                                   "target_base": "b" * 40}}
    value["publication"] = {"intent_sha256": "6" * 64, "state": "confirmed",
                            "candidate": "c" * 40, "tree": "d" * 40,
                            "expected_base": "b" * 40,
                            "target_ref": "refs/heads/dev-006",
                            "observed_target": "c" * 40, "pr": 71,
                            "bundle_id": "bundle-" + "1" * 24}
    value["completion"] = {"source_head": "a" * 40}
    assert check(sign(value, key=KEY), current)["bundle_id"] == current.bundle_id
    value["publication"]["state"] = "unknown"
    with pytest.raises(DomainError):
        check(sign(value, key=KEY), current)
