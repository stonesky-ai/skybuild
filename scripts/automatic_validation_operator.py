#!/usr/bin/env python3
"""Post exact pre-freeze source checks and independent review from trusted roles.

This operator never runs candidate code. Focused test PASS results require a
separately signed, isolated runner receipt before the validator role may post.
The reviewer role uses its own API identity and HMAC key, and independently
rechecks the worker tree against the preapproved patch before signing.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]

from skybuild.automatic_validation import static_submission
from skybuild.client import Client, ca_file_sha256
from skybuild.fleet_preflight import _resolved_addresses, _token_from_file
from skybuild.manual_dispatch import _private_endpoint
from skybuild.store import Store
from skybuild.validation_adapter import record_validation_result
from skybuild.workflow import (Place, ResultState, ValidationResult,
                               ValidationStage, _result_current)

import gate_policy
from automatic_promotion_conductor import authorize, _checked_dir, _existing_or_save
from trusted_integration_producer import _key, _read, _save, _source


class ValidationOperatorError(ValueError):
    pass


def _configuration(policy: dict, state: Path, checkout: Path) -> dict:
    path = state / "validation-runtime.json"
    gate_policy.outside(path, checkout)
    raw = gate_policy.private(path)
    if gate_policy.digest(raw) != policy["delivery"]["validation_runtime_sha256"]:
        raise ValidationOperatorError("Validator runtime config differs from owner permit")
    value = gate_policy.fields(gate_policy.parse(raw),
                               {"schema", "validation_token_file", "reviewer_tokens",
                                "reviewer_for_task"})
    if (value["schema"] != "skybuild.validation-runtime.v1"
            or not isinstance(value["reviewer_tokens"], dict)
            or not isinstance(value["reviewer_for_task"], dict)
            or set(value["reviewer_for_task"]) != {task["task_id"] for task in policy["tasks"]}):
        raise ValidationOperatorError("Validator role configuration differs")
    for token in [value["validation_token_file"], *value["reviewer_tokens"].values()]:
        gate_policy.outside(Path(token), checkout)
    return value


def _api(checkout: Path, state: Path, policy: dict, token_path: Path) -> Client:
    from bundle_publisher import _read_trust_config
    _, _, source, _, config = _read_trust_config(
        state / "publisher-trust.json", policy["delivery"]["publisher_trust_sha256"], checkout)
    if source != policy["delivery"]["publisher_source_sha"]:
        raise ValidationOperatorError("Publisher API endpoint source differs")
    ca = Path(config["ca_file"])
    if ca_file_sha256(ca) != config["ca_sha256"]:
        raise ValidationOperatorError("API CA differs from publisher pin")
    return Client(_private_endpoint(config["base_url"], _resolved_addresses),
                  _token_from_file(token_path), retries=0, timeout=10, trust_env=False,
                  ca_file=ca, expected_ca_sha256=config["ca_sha256"])


def _result(token, stage: ValidationStage, producer: str,
            artifact: str, *, check_id: str) -> ValidationResult:
    return ValidationResult(token.project_id, token.task_id, stage, ResultState.PASSED,
                            attempt_id=token.attempt_id, source_head=token.source_head,
                            target_base=token.target_base,
                            input_generation=token.input_generation,
                            definition_revision=token.definition_revision,
                            policy_version=token.policy_version,
                            claim_fence=token.claim_fence, producer=producer,
                            check_id=check_id, tool_version="auto-validation-v1",
                            artifacts=(artifact,))


def _record_once(client: Client, state: Path, result: ValidationResult) -> bool:
    """One POST maximum; an uncertain reply is reconciled through task history."""
    prefix = gate_policy.digest((result.task_id + ":" + result.stage.value).encode())
    body_path = state / (prefix + ".validation.result.json")
    intent_path = state / (prefix + ".validation.intent.json")
    body = result.to_dict()
    _existing_or_save(body_path, body)
    if not intent_path.exists():
        view = client.task_workflow(result.project_id, result.task_id)
        token = Store.workflow_token(view["task"])
        if (view.get("token") != token.to_dict() or token.place != Place.VALIDATING
                or token.source_head != result.source_head or token.attempt_id != result.attempt_id
                or token.claim_fence != result.claim_fence):
            raise ValidationOperatorError("Validation result differs from current author attempt")
        operation = "auto-validate-" + prefix[:40]
        intent = {"schema": "skybuild.validation-intent.v1", "task_id": result.task_id,
                  "stage": result.stage.value, "operation_id": operation,
                  "expected_revision": token.revision,
                  "result_sha256": gate_policy.digest(gate_policy.canonical(body))}
        _save(intent_path, intent)
        record_validation_result(client, result, expected_revision=token.revision,
                                 idempotency_key=operation)
    intent = _read(intent_path)
    if (intent.get("result_sha256") != gate_policy.digest(gate_policy.canonical(body))
            or intent.get("stage") != result.stage.value or intent.get("task_id") != result.task_id):
        raise ValidationOperatorError("Retained validation intent differs")
    expected_revision = intent["expected_revision"] + 1
    history = client.task_history(result.project_id, result.task_id, limit=100,
                                  offset=max(0, expected_revision - 100))
    exact = [row for row in history if row.get("revision") == expected_revision]
    if len(exact) == 1 and (exact[0].get("operation") == "workflow.validation_result"
                            and exact[0].get("event_facts", {}).get("operation_id") == intent["operation_id"]
                            and exact[0].get("event_facts", {}).get("result") == body):
        current = client.task_workflow(result.project_id, result.task_id)
        token = Store.workflow_token(current["task"])
        if (current.get("token") != token.to_dict() or token.place not in
                {Place.VALIDATING, Place.INTEGRATING, Place.DONE}
                or result not in token.evidence or not _result_current(token, result)):
            raise ValidationOperatorError("Journal result is no longer current")
        return True
    if exact:
        raise ValidationOperatorError("Current journal revision conflicts with retained result")
    return False


def _static(client: Client, checkout: Path, policy: dict, state: Path,
            task: dict, principal: str) -> dict | None:
    view = client.task_workflow(policy["project_id"], task["task_id"])
    token = Store.workflow_token(view["task"])
    if token.place == Place.WORKING:
        return None
    patch = gate_policy.private(Path(task["patch_path"]))
    report = static_submission(checkout, view, task, target_ref=policy["target_ref"], patch=patch)
    path = state / (gate_policy.digest(task["task_id"].encode()) + ".static.json")
    _existing_or_save(path, report)
    artifact = str(path) + "#sha256=" + gate_policy.digest(gate_policy.private(path))
    for stage in (ValidationStage.SCANS, ValidationStage.NEEDS_REBASE):
        result = _result(token, stage, principal, artifact, check_id="exact-approved-patch-" + stage.value)
        if not _record_once(client, state, result):
            return None
    return report


def _focused(client: Client, checkout: Path, policy: dict, trust: dict,
             state: Path, task: dict, principal: str, stage: str,
             permit_sha256: str, conductor_intent_sha256: str) -> bool:
    """Record a real unit/long PASS only from one verified isolated receipt."""
    if stage not in {"unit", "long"}:
        raise ValidationOperatorError("Focused stage is outside the reviewed pair")
    prefix = gate_policy.digest(task["task_id"].encode())
    reference = gate_policy.fields(_read(state / (prefix + "." + stage
                                            + ".focused.reference.json")),
                                   {"path", "sha256"})
    receipt_path = Path(reference["path"])
    gate_policy.outside(receipt_path, checkout)
    raw = gate_policy.private(receipt_path)
    if gate_policy.digest(raw) != gate_policy.sha(reference["sha256"]):
        raise ValidationOperatorError("Focused receipt bytes differ from retained stage proof")
    view = client.task_workflow(policy["project_id"], task["task_id"])
    token = Store.workflow_token(view["task"])
    if (view.get("token") != token.to_dict() or token.place != Place.VALIDATING
            or token.pending_action is not None or token.superseded
            or token.responsible != task["worker_id"]
            or token.source_branch != task["task_branch"]
            or token.target_base != policy["base_sha"]):
        raise ValidationOperatorError("Focused receipt no longer matches live worker attempt")
    position = next(index for index, item in enumerate(policy["tasks"])
                    if item["task_id"] == task["task_id"])
    name = f"focused-{position}-{stage}"
    input_path = state / (name + ".input.json")
    input_raw = gate_policy.private(input_path)
    signed = gate_policy.parse(input_raw)
    input_payload = gate_policy.verify(signed, gate_policy.INTEGRATION,
                                       trust["integration"])
    if (input_payload.get("head_sha") != token.source_head
            or input_payload.get("task_id") != task["task_id"]
            or input_payload.get("stage") != stage):
        raise ValidationOperatorError("Focused signed input differs from current worker")
    consumed = state / (gate_policy.digest(policy["permit_id"].encode())
                        + "." + name + ".consumed.json")
    verified = gate_policy.verify_focused(gate_policy.parse(raw), trust["validation"], {
        "permit_id": policy["permit_id"], "policy_sha256": permit_sha256,
        "consumption_sha256": gate_policy.digest(gate_policy.private(consumed)),
        "input_sha256": gate_policy.digest(input_raw),
        "conductor_intent_sha256": conductor_intent_sha256,
        "project_id": policy["project_id"], "task_id": task["task_id"],
        "assignment_id": task["assignment_id"], "worker_id": task["worker_id"],
        "brief_sha256": task["brief_sha256"],
        "approved_patch_sha256": task["approved_patch_sha256"],
        "source_head": token.source_head,
        "source_tree": input_payload["candidate_tree"],
        "base_sha": policy["base_sha"], "workflow": input_payload["workflow"],
        "stage": stage, "profile": task["focused_profiles"][stage],
        "runner_source": policy["runner_source"],
        "execution_host": policy["execution_host"], "images": policy["images"]})
    if verified["workflow"] != {
            "attempt_id": token.attempt_id, "claim_fence": token.claim_fence,
            "input_generation": token.input_generation,
            "definition_revision": token.definition_revision,
            "policy_version": token.policy_version,
            "source_sha": token.source_head, "base_sha": token.target_base}:
        raise ValidationOperatorError("Focused evidence is stale for live workflow tuple")
    result_stage = (ValidationStage.UNIT_TESTS if stage == "unit"
                    else ValidationStage.LONG_TESTS)
    artifact = str(receipt_path) + "#sha256=" + reference["sha256"]
    result = _result(token, result_stage, principal, artifact,
                     check_id=task["focused_profiles"][stage])
    return _record_once(client, state, result)


def _review(client: Client, checkout: Path, policy: dict, trust: dict, state: Path,
            task: dict, signer: dict) -> bool:
    view = client.task_workflow(policy["project_id"], task["task_id"])
    token = Store.workflow_token(view["task"])
    if token.place != Place.VALIDATING:
        return False
    required = {ValidationStage.UNIT_TESTS, ValidationStage.SCANS,
                ValidationStage.LONG_TESTS, ValidationStage.NEEDS_REBASE}
    for stage in required:
        results = [item for item in token.evidence if item.stage == stage
                   and _result_current(token, item) and item.state == ResultState.PASSED]
        if len(results) != 1:
            return False
    proof = static_submission(checkout, view, task, target_ref=policy["target_ref"],
                              patch=gate_policy.private(Path(task["patch_path"])))
    artifact_path = state / (gate_policy.digest(task["task_id"].encode()) + ".review-report.json")
    report = {"schema": "skybuild.preapproved-patch-independent-review.v1",
              "verdict": "pass", "task_id": task["task_id"],
              "source_head": token.source_head, "source_branch": token.source_branch,
              "base_sha": token.target_base,
              "approved_patch_sha256": task["approved_patch_sha256"],
              "approved_tree": proof["approved_tree"],
              "reviewer_principal": signer["principal"],
              "scope": "Exact preapproved patch bytes and current signed validation evidence",
              "validation": {stage.value: [item.artifacts for item in token.evidence
                                          if item.stage == stage and _result_current(token, item)]
                             for stage in sorted(required, key=lambda value: value.value)}}
    _existing_or_save(artifact_path, report)
    artifact_sha = gate_policy.digest(gate_policy.private(artifact_path))
    artifact_uri = str(artifact_path) + "#sha256=" + artifact_sha
    payload = {"project_id": policy["project_id"], "task_id": task["task_id"],
               "source_head": token.source_head, "source_branch": token.source_branch,
               "base_sha": token.target_base, "attempt_id": token.attempt_id,
               "claim_fence": token.claim_fence, "input_generation": token.input_generation,
               "definition_revision": token.definition_revision,
               "policy_version": token.policy_version,
               "source_sha": token.source_head, "reviewer_principal": signer["principal"],
               "verdict": "pass", "artifact_uri": artifact_uri,
               "artifact_sha256": artifact_sha,
               "issued_at": datetime.now(timezone.utc).isoformat()}
    envelope_path = state / (gate_policy.digest(task["task_id"].encode()) + ".review.json")
    if envelope_path.exists():
        envelope = _read(envelope_path)
        verified = gate_policy.verify(envelope, gate_policy.REVIEW, signer)
        if {name: verified[name] for name in payload if name != "issued_at"} != {
                name: payload[name] for name in payload if name != "issued_at"}:
            raise ValidationOperatorError("Retained independent review signature differs")
    else:
        material = _key(Path(signer["key_path"]))
        envelope = {"schema": gate_policy.REVIEW, "key_id": signer["key_id"],
                    "principal": signer["principal"], "payload": payload,
                    "signature": hmac.new(material, gate_policy.REVIEW.encode() + b"\0"
                                          + gate_policy.canonical(payload), hashlib.sha256).hexdigest()}
        _save(envelope_path, envelope)
    result = _result(token, ValidationStage.CODE_REVIEW, signer["principal"], artifact_uri,
                     check_id="independent-exact-preapproved-patch-review")
    return _record_once(client, state, result)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=("static", "focused", "review"))
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--permit", type=Path, required=True)
    parser.add_argument("--permit-sha256", required=True)
    parser.add_argument("--trust", type=Path, required=True)
    parser.add_argument("--trust-sha256", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--stage", choices=("unit", "long"))
    args = parser.parse_args(argv)
    try:
        policy, trust, _ = authorize(args.checkout, args.permit, args.permit_sha256,
                                     args.trust, args.trust_sha256)
        state = _checked_dir(Path(policy["delivery"]["state_dir"]))
        config = _configuration(policy, state, args.checkout)
        tasks = [task for task in policy["tasks"] if task["task_id"] == args.task_id]
        if len(tasks) != 1:
            raise ValidationOperatorError("Task is absent from owner permit")
        task = tasks[0]
        if args.role in {"static", "focused"}:
            principal = trust["validation"]["principal"]
            token_path = Path(config["validation_token_file"])
            signer = None
        else:
            principal = config["reviewer_for_task"][args.task_id]
            signers = [item for item in trust["reviewers"] if item["principal"] == principal]
            if len(signers) != 1:
                raise ValidationOperatorError("Reviewer principal lacks independent trust key")
            signer = signers[0]
            token_path = Path(config["reviewer_tokens"][principal])
        _source(args.checkout, policy["delivery"]["conductor_source_sha"])
        with _api(args.checkout, state, policy, token_path) as client:
            identity = client.whoami()
            if identity.get("principal_id") != principal or identity.get("is_admin") is not True:
                raise ValidationOperatorError("Validation role differs from authenticated API principal")
            if args.role == "static":
                if args.stage is not None:
                    raise ValidationOperatorError("Static role cannot select a focused stage")
                result = _static(client, args.checkout, policy, state, task, principal)
            elif args.role == "focused":
                if args.stage is None:
                    raise ValidationOperatorError("Focused role requires exact stage")
                result = _focused(client, args.checkout, policy, trust, state, task,
                                  principal, args.stage, args.permit_sha256,
                                  gate_policy.digest(gate_policy.private(
                                      state / (gate_policy.digest(policy["permit_id"].encode())
                                               + ".conductor.consumed.json"))))
            else:
                if args.stage is not None:
                    raise ValidationOperatorError("Review role cannot select a focused stage")
                result = _review(client, args.checkout, policy, trust, state, task, signer)
        print(json.dumps({"role": args.role, "task_id": args.task_id,
                          "stage": args.stage,
                          "confirmed": result is not None and result is not False}, sort_keys=True))
        return 0 if result is not None and result is not False else 3
    except (OSError, ValueError, KeyError, TypeError, gate_policy.PolicyError) as error:
        print(json.dumps({"ok": False, "error": type(error).__name__}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
