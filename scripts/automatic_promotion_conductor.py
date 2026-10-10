#!/usr/bin/env python3
"""Resume one owner-approved, two-worker delivery through durable trusted stages.

The gate policy is the sole approval. This process never creates a review
verdict or retries an uncertain external mutation. An existing stage intent
can only be reconciled by GET observations of the exact effect.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]

from skybuild.automatic_progression import (ProgressionError, bundle_manifest,
                                            frozen_gate_input, gate_members,
                                            reviewed_members)
from skybuild.automatic_focused import (FocusedError, candidate_checkout,
                                        focused_payload, signed_input)
from skybuild.client import Client, ca_file_sha256
from skybuild.fleet_preflight import _resolved_addresses, _token_from_file
from skybuild.manual_dispatch import _private_endpoint
from skybuild.store import Store
from skybuild.workflow import Place, ResultState, ValidationStage, _result_current

import gate_policy
import prepare_bundle
from trusted_integration_producer import (_bundle, _key, _read, _save, _source,
                                          accept, freeze, submit)


class ConductorError(ValueError):
    pass


def _checked_dir(path: Path) -> Path:
    if not path.is_absolute() or path.resolve() != path:
        raise ConductorError("Conductor state path must be canonical")
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ConductorError("Conductor state must be an owned mode-0700 directory")
    return path


def _existing_or_save(path: Path, value: dict) -> dict:
    if path.exists():
        observed = _read(path)
        if observed != value:
            raise ConductorError("Durable stage input changed")
        return observed
    _save(path, value)
    return value


def authorize(checkout: Path, permit_path: Path, permit_sha: str,
              trust_path: Path, trust_sha: str) -> tuple[dict, dict, str]:
    """Verify owner approval, then consume once before any external write."""
    for path in (permit_path, trust_path):
        gate_policy.outside(path, checkout)
    trust_data = gate_policy.private(trust_path)
    if gate_policy.digest(trust_data) != gate_policy.sha(trust_sha):
        raise ConductorError("Gate trust differs from owner pin")
    trust = gate_policy.fields(gate_policy.parse(trust_data),
                               {"schema", "approval", "integration", "validation",
                                "reviewers", "consume_dir"})
    if trust["schema"] != gate_policy.TRUST:
        raise ConductorError("Gate trust schema differs")
    approved = gate_policy.key_spec(trust["approval"])
    integration = gate_policy.key_spec(trust["integration"])
    validation = gate_policy.key_spec(trust["validation"])
    reviewers = [gate_policy.key_spec(item) for item in trust["reviewers"]]
    keys = [approved, integration, validation, *reviewers]
    if (len(reviewers) < 2 or len({item["principal"] for item in keys}) != len(keys)
            or len({item["key_id"] for item in keys}) != len(keys)
            or len({item["key_path"] for item in keys}) != len(keys)):
        raise ConductorError("Trust principals and keys are not distinct")
    for item in keys:
        gate_policy.outside(Path(item["key_path"]), checkout)
    data = gate_policy.private(permit_path)
    if gate_policy.digest(data) != gate_policy.sha(permit_sha):
        raise ConductorError("Permit differs from exact owner pin")
    policy = gate_policy.verify(gate_policy.parse(data), gate_policy.APPROVAL, approved)
    gate_policy.fields(policy, {"schema", "permit_id", "project_id", "target_ref", "base_sha",
                                "tasks", "source_profile", "runner_source", "images",
                                "execution_host", "resource_limits", "attestation_key_id",
                                "gate_profile", "delivery", "usage", "host_watch",
                                "issued_at", "expires_at", "max_runs"})
    now = datetime.now(timezone.utc)
    if (policy["schema"] != gate_policy.POLICY or policy["source_profile"] != gate_policy.PROFILE
            or policy["project_id"] != "skybuild" or type(policy["max_runs"]) is not int
            or policy["max_runs"] != 1
            or not gate_policy.timestamp(policy["issued_at"]) <= now < gate_policy.timestamp(policy["expires_at"])):
        raise ConductorError("Bounded approval is invalid or expired")
    if not isinstance(policy["tasks"], list) or len(policy["tasks"]) != 2:
        raise ConductorError("Permit needs two exact tasks")
    for task in policy["tasks"]:
        gate_policy.fields(task, {"task_id", "assignment_id", "worker_id", "task_branch",
                                  "brief_sha256", "approved_patch_sha256", "patch_path",
                                  "owned_paths", "base_sha", "definition_revision", "policy_version",
                                  "focused_profiles", "brief_path"})
        if task["base_sha"] != policy["base_sha"]:
            raise ConductorError("Worker base differs from approved target")
        brief_path = Path(task["brief_path"])
        gate_policy.outside(brief_path, checkout)
        if gate_policy.digest(gate_policy.private(brief_path)) != task["brief_sha256"]:
            raise ConductorError("Amended source brief differs from owner permit")
    delivery = gate_policy.fields(policy["delivery"], {"conductor_source_sha",
                                                        "integration_source_sha", "publisher_source_sha",
                                                        "publisher_trust_sha256", "gate_trust_sha256",
                                                        "validation_runtime_sha256",
                                                        "pr_source_ref", "state_dir"})
    if (delivery["gate_trust_sha256"] != trust_sha
            or len({delivery[name] for name in ("conductor_source_sha", "integration_source_sha",
                                                "publisher_source_sha")}) != 1
            or delivery["pr_source_ref"] == policy["target_ref"]
            or not re.fullmatch(r"task/[A-Za-z0-9._/-]+", delivery["pr_source_ref"])):
        raise ConductorError("Exact combined source or delivery branch differs")
    _source(checkout, delivery["conductor_source_sha"])
    state = _checked_dir(Path(delivery["state_dir"]))
    if state != _checked_dir(Path(trust["consume_dir"])) or state.is_relative_to(checkout):
        raise ConductorError("Conductor state differs from gate trust")
    gate_policy.Authorization(policy, state, "").check(starting=True)
    permit_id = gate_policy.text(policy["permit_id"])
    record_path = state / (gate_policy.digest(permit_id.encode()) + ".conductor.consumed.json")
    if record_path.exists():
        record = _read(record_path)
        if (set(record) != {"schema", "permit_id", "policy_sha256", "conductor_source_sha",
                            "integration_source_sha", "consumed_at", "state"}
                or record["schema"] != "skybuild.conductor-permit-consumption.v1"
                or record["permit_id"] != permit_id or record["policy_sha256"] != permit_sha
                or record["state"] != "consumed_hold_on_unknown"
                or record["conductor_source_sha"] != delivery["conductor_source_sha"]
                or record["integration_source_sha"] != delivery["integration_source_sha"]
                or not gate_policy.timestamp(policy["issued_at"])
                <= gate_policy.timestamp(record["consumed_at"]) <= now):
            raise ConductorError("Existing conductor consumption differs")
    else:
        record = {"schema": "skybuild.conductor-permit-consumption.v1",
                  "permit_id": permit_id, "policy_sha256": permit_sha,
                  "conductor_source_sha": delivery["conductor_source_sha"],
                  "integration_source_sha": delivery["integration_source_sha"],
                  "consumed_at": now.isoformat(), "state": "consumed_hold_on_unknown"}
        _save(record_path, record)
    return policy, trust, gate_policy.digest(gate_policy.private(record_path))


def _reviews(policy: dict, trust: dict, state: Path, checkout: Path) -> list[dict] | None:
    approved = []
    for task in policy["tasks"]:
        path = state / (gate_policy.digest(task["task_id"].encode()) + ".review.json")
        if not path.exists():
            return None
        envelope = gate_policy.parse(gate_policy.private(path))
        matching = []
        for signer in trust["reviewers"]:
            if envelope.get("principal") == signer["principal"]:
                matching.append(gate_policy.verify(envelope, gate_policy.REVIEW, signer))
        if len(matching) != 1:
            raise ConductorError("Independent signed review principal is not trusted")
        review = matching[0]
        if (review.get("task_id") != task["task_id"] or review.get("project_id") != policy["project_id"]
                or review.get("source_branch") != task["task_branch"]
                or review.get("base_sha") != policy["base_sha"]
                or review.get("reviewer_principal") != envelope["principal"]
                or review.get("verdict") != "pass"):
            raise ConductorError("Independent review differs from approved task")
        artifact = review.get("artifact_uri")
        sha = review.get("artifact_sha256")
        if not isinstance(artifact, str) or not artifact.endswith("#sha256=" + sha):
            raise ConductorError("Signed review artifact URI differs")
        artifact_path = Path(artifact[:-(len(sha) + 8)])
        gate_policy.outside(artifact_path, checkout)
        if gate_policy.digest(gate_policy.private(artifact_path)) != gate_policy.sha(sha):
            raise ConductorError("Independent review artifact bytes differ")
        approved.append({"task_id": task["task_id"], "worker": task["worker_id"],
                         "branch": task["task_branch"].removeprefix("refs/heads/"),
                         "reviewer": envelope["principal"], "review_path": str(artifact_path)})
    return approved


def _prepared(client: Client, policy: dict, trust: dict, state: Path,
              checkout: Path, permit_path: Path) -> tuple[dict, list[dict]] | None:
    members_path = state / "reviewed-members.json"
    if members_path.exists():
        members = _read(members_path)["members"]
    else:
        expected = _reviews(policy, trust, state, checkout)
        if expected is None:
            return None
        members = reviewed_members(client, policy["project_id"], expected,
                                   base_sha=policy["base_sha"])
        if members is None:
            return None
        _save(members_path, {"members": members})
    manifest = bundle_manifest(members, target_ref=policy["target_ref"],
                               base_sha=policy["base_sha"], policy_evidence=permit_path)
    manifest_path = state / "bundle-manifest.json"
    _existing_or_save(manifest_path, manifest)
    report_path = state / "prepared" / "report.json"
    if report_path.exists():
        bundle = _bundle(checkout, state / "prepared")
        report = json.loads(report_path.read_bytes())
        if (not isinstance(report, dict) or report.get("candidate_head") != bundle["candidate_commit"]
                or report.get("candidate_tree") != bundle["candidate_tree"]):
            raise ConductorError("Retained candidate report differs from frozen bundle")
    else:
        report = prepare_bundle.prepare(checkout, manifest_path, state / "prepared")
    if not report.get("ok") or report.get("candidate_head") is None:
        raise ConductorError("Reviewed bundle preparation has no exact candidate")
    return report, members


def _role_stage(checkout: Path, policy: dict, permit: Path, permit_sha: str,
                trust_path: Path, trust_sha: str, task_id: str, role: str,
                *, stage: str | None = None) -> bool:
    """Run a distinct trusted evaluator process with its role-specific token."""
    environment = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    environment.update(GIT_TERMINAL_PROMPT="0",
                       PYTHONPATH=os.pathsep.join((str(checkout / "src"),
                                                   str(checkout / "scripts"), str(checkout))),
                       UV_PROJECT_ENVIRONMENT=str(checkout / ".venv"))
    argv = [str(checkout / ".venv/bin/python"), "-P",
            str(checkout / "scripts/automatic_validation_operator.py"), role,
            "--checkout", str(checkout), "--permit", str(permit),
            "--permit-sha256", permit_sha, "--trust", str(trust_path),
            "--trust-sha256", trust_sha, "--task-id", task_id]
    if stage is not None:
        argv += ["--stage", stage]
    result = subprocess.run(argv, cwd=checkout, env=environment, text=True,
                            capture_output=True, timeout=120)
    if result.returncode not in {0, 3} or len(result.stdout) > 4096:
        raise ConductorError("Separate trusted validation role failed")
    output = json.loads(result.stdout)
    if (output.get("role") != role or output.get("task_id") != task_id
            or output.get("stage") != stage
            or output.get("confirmed") is not (result.returncode == 0)):
        raise ConductorError("Separate validation role returned inconsistent outcome")
    return result.returncode == 0


def _focused_stage(client: Client, checkout: Path, policy: dict, trust: dict,
                   state: Path, task: dict, position: int, stage: str,
                   permit: Path, permit_sha: str, trust_path: Path, trust_sha: str,
                   conductor_intent_sha: str, runner_pins: dict) -> Path | None:
    """Consume one reviewed isolated profile, verify its signed real result."""
    from bundle_publisher import _candidate_archive_sha256
    from isolated_full_test_gate import execute_policy

    view = client.task_workflow(policy["project_id"], task["task_id"])
    token = Store.workflow_token(view["task"])
    if token.place == Place.WORKING:
        return None
    if (view.get("token") != token.to_dict() or token.place != Place.VALIDATING
            or token.pending_action is not None or token.superseded):
        raise ConductorError("Focused candidate is not the current submitted worker attempt")
    head = token.source_head
    candidate = candidate_checkout(checkout, state, head)
    tree = _command(["git", "rev-parse", head + "^{tree}"], candidate)
    archive = _candidate_archive_sha256(candidate, head)
    history = runner_pins["candidate_history_sha256"]
    name = f"focused-{position}-{stage}"
    payload = focused_payload(policy, task, token, stage=stage,
                              permit_sha256=permit_sha,
                              conductor_intent_sha256=conductor_intent_sha,
                              source_tree=tree, archive_sha256=archive,
                              history_sha256=history)
    input_path = state / (name + ".input.json")
    signer = trust["integration"]
    if input_path.exists():
        envelope = gate_policy.parse(gate_policy.private(input_path))
        observed = gate_policy.verify(envelope, gate_policy.INTEGRATION, signer)
        if {key: value for key, value in observed.items() if key != "submitted_at"} != {
                key: value for key, value in payload.items() if key != "submitted_at"}:
            raise ConductorError("Retained focused input differs from current worker attempt")
    else:
        envelope = signed_input(payload, signer, _key(Path(signer["key_path"])),
                                gate_policy.INTEGRATION)
        _save(input_path, envelope)
        observed = payload
    profile = policy["gate_profile"]
    predicate = {"target_ref": policy["target_ref"],
                 "target_base": policy["base_sha"], "candidate_commit": head,
                 "candidate_tree": tree, "candidate_archive_sha256": archive,
                 "candidate_history_sha256": history,
                 "execution_host": policy["execution_host"],
                 **profile, **policy["images"]}
    predicate_path = state / (name + ".predicate.json")
    _existing_or_save(predicate_path, predicate)
    output_dir = state / (name + ".runs")
    output_dir.mkdir(mode=0o700, exist_ok=True)
    _checked_dir(output_dir)
    intent_path = state / (name + ".call.intent.json")
    intent = {"schema": "skybuild.focused-call-intent.v1", "permit_id": policy["permit_id"],
              "stage": name, "source_head": head,
              "input_sha256": gate_policy.digest(gate_policy.private(input_path)),
              "predicate_sha256": gate_policy.digest(gate_policy.private(predicate_path))}
    if not intent_path.exists():
        _save(intent_path, intent)
        result = execute_policy(candidate, predicate_path, permit, permit_sha,
                                trust_path, trust_sha, input_path,
                                Path(trust["validation"]["key_path"]),
                                trust["validation"]["key_id"], output_dir,
                                focused_stage=name)
        if (result.get("validation_passed") is not True
                or result.get("cleanup_confirmed") is not True
                or result.get("failure") is not None):
            raise ConductorError("Isolated focused validation failed; one-shot stage remains held")
    elif _read(intent_path) != intent:
        raise ConductorError("Retained focused call differs from current attempt")
    paths = sorted(output_dir.glob("run-*/attestation.json"))
    if len(paths) != 1:
        return None
    receipt_raw = gate_policy.private(paths[0])
    consumed = state / (gate_policy.digest(policy["permit_id"].encode())
                        + "." + name + ".consumed.json")
    consumption_sha = gate_policy.digest(gate_policy.private(consumed))
    gate_policy.verify_focused(gate_policy.parse(receipt_raw), trust["validation"], {
        "permit_id": policy["permit_id"], "policy_sha256": permit_sha,
        "consumption_sha256": consumption_sha,
        "input_sha256": gate_policy.digest(gate_policy.private(input_path)),
        "conductor_intent_sha256": conductor_intent_sha,
        "project_id": policy["project_id"], "task_id": task["task_id"],
        "assignment_id": task["assignment_id"], "worker_id": task["worker_id"],
        "brief_sha256": task["brief_sha256"],
        "approved_patch_sha256": task["approved_patch_sha256"],
        "source_head": head, "source_tree": tree, "base_sha": policy["base_sha"],
        "workflow": observed["workflow"], "stage": stage,
        "profile": task["focused_profiles"][stage],
        "runner_source": policy["runner_source"],
        "execution_host": policy["execution_host"], "images": policy["images"]})
    reference = {"path": str(paths[0]), "sha256": gate_policy.digest(receipt_raw)}
    reference_path = state / (gate_policy.digest(task["task_id"].encode())
                              + "." + stage + ".focused.reference.json")
    _existing_or_save(reference_path, reference)
    return paths[0]


def _freeze_both(client: Client, policy: dict, trust: dict, state: Path,
                 checkout: Path, members: list[dict]) -> list[dict] | None:
    bundle = _bundle(checkout, state / "prepared")
    signer = trust["integration"]
    key = _key(Path(signer["key_path"]))
    packets = []
    for member in members:
        task_id = member["task_id"]
        suffix = gate_policy.digest(task_id.encode())
        packet_path = state / (suffix + ".freeze.packet.json")
        if packet_path.exists():
            packet = _read(packet_path)
        else:
            view = client.task_workflow(policy["project_id"], task_id)
            operation_id = "auto-freeze-" + gate_policy.digest(
                (policy["permit_id"] + ":" + task_id).encode())[:32]
            packet = freeze(view, bundle, operation_id=operation_id,
                            producer=signer["principal"], key_id=signer["key_id"], key=key)
            _save(packet_path, packet)
        packets.append(packet)
        result = submit(client, policy["project_id"], task_id, packet_path,
                        state / (suffix + ".freeze.intent.json"))
        if not result.get("confirmed"):
            return None
    return packets


def _command(argv: list[str], checkout: Path, *, timeout: int = 90) -> str:
    environment = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    environment.update(GIT_TERMINAL_PROMPT="0", GH_PROMPT_DISABLED="1")
    result = subprocess.run(argv, cwd=checkout, env=environment, text=True,
                            capture_output=True, timeout=timeout)
    if result.returncode or len(result.stdout) > 512 * 1024:
        raise ConductorError("Trusted GitHub operation failed or exceeded bound")
    return result.stdout.strip()


def _remote_branch(checkout: Path, ref: str) -> str | None:
    output = _command(["git", "-c", "core.hooksPath=/dev/null", "ls-remote",
                       "--refs", "origin", ref], checkout)
    if not output:
        return None
    columns = output.split()
    if len(columns) != 2 or columns[1] != ref or not re.fullmatch(r"[0-9a-f]{40}", columns[0]):
        raise ConductorError("Remote returned ambiguous exact branch")
    return columns[0]


def _github_pr(checkout: Path, branch: str) -> dict | None:
    query = f"repos/stonesky-ai/skybuild/pulls?state=all&head=stonesky-ai:{branch}&per_page=100"
    observed = json.loads(_command(["gh", "api", query], checkout))
    if not isinstance(observed, list) or len(observed) > 1:
        raise ConductorError("GitHub pull request observation is ambiguous")
    return observed[0] if observed else None


def _pr_stage(checkout: Path, policy: dict, state: Path, report: dict, bundle: dict) -> int | None:
    """One candidate push and one PR create; after intent, all retries are GET only."""
    branch = policy["delivery"]["pr_source_ref"]
    ref = "refs/heads/" + branch
    candidate = report["candidate_head"]
    push_intent = state / "candidate-push.intent.json"
    push_value = {"schema": "skybuild.candidate-push-intent.v1", "permit_id": policy["permit_id"],
                  "bundle_id": bundle["bundle_id"], "candidate_commit": candidate,
                  "expected_remote_old": None, "pr_source_ref": ref,
                  "target_ref": policy["target_ref"], "base_sha": policy["base_sha"]}
    if not push_intent.exists():
        if _remote_branch(checkout, ref) is not None:
            raise ConductorError("Approved bundle PR source branch already exists")
        _save(push_intent, push_value)
        _command(["git", "-c", "core.hooksPath=/dev/null", "push", "--porcelain",
                  "--force-with-lease=" + ref + ":", "origin", candidate + ":" + ref], checkout)
    elif _read(push_intent) != push_value:
        raise ConductorError("Candidate push intent differs")
    remote_head = _remote_branch(checkout, ref)
    if remote_head is None:
        return None
    if remote_head != candidate:
        raise ConductorError("Published bundle source branch differs from approved candidate")
    pr_intent = state / "bundle-pr.intent.json"
    title = "SkyBuild reviewed two-worker bundle " + bundle["bundle_id"]
    body = ("Exact two-worker SkyBuild bundle. Candidate: " + candidate + "\n"
            "Base: " + policy["base_sha"] + "\nBundle: " + bundle["bundle_id"] + "\n")
    pr_value = {"schema": "skybuild.bundle-pr-intent.v1", "permit_id": policy["permit_id"],
                "bundle_id": bundle["bundle_id"], "head_ref": branch,
                "head_sha": candidate, "base_ref": policy["target_ref"].removeprefix("refs/heads/"),
                "base_sha": policy["base_sha"], "title": title, "body": body}
    if not pr_intent.exists():
        if _github_pr(checkout, branch) is not None:
            raise ConductorError("Bundle PR already exists before one-shot intent")
        _save(pr_intent, pr_value)
        _command(["gh", "api", "--method", "POST", "repos/stonesky-ai/skybuild/pulls",
                  "-f", "title=" + title, "-f", "head=" + branch,
                  "-f", "base=" + pr_value["base_ref"], "-f", "body=" + body], checkout)
    elif _read(pr_intent) != pr_value:
        raise ConductorError("Bundle PR intent differs")
    observed = _github_pr(checkout, branch)
    if observed is None:
        return None
    head, base = observed.get("head"), observed.get("base")
    head_repo = head.get("repo") if isinstance(head, dict) else None
    base_repo = base.get("repo") if isinstance(base, dict) else None
    confirmation_path = state / "bundle-pr.confirmed.json"
    earlier = _read(confirmation_path) if confirmation_path.exists() else None
    if (not isinstance(head, dict) or not isinstance(base, dict)
            or not isinstance(head_repo, dict) or not isinstance(base_repo, dict)
            or observed.get("state") not in {"open", "closed"}
            or observed.get("draft") is not False
            or head.get("sha") != candidate or head.get("ref") != branch
            or base.get("ref") != pr_value["base_ref"]
            or head_repo.get("full_name") != "stonesky-ai/skybuild"
            or base_repo.get("full_name") != "stonesky-ai/skybuild"
            or type(observed.get("number")) is not int):
        raise ConductorError("Observed bundle PR differs from exact reviewed candidate/base")
    confirmation = {"schema": "skybuild.bundle-pr-confirmed.v1", "pr": observed["number"],
                    "candidate_commit": candidate, "base_sha": policy["base_sha"],
                    "head_ref": branch, "base_ref": pr_value["base_ref"]}
    if earlier is None:
        if observed["state"] != "open" or base.get("sha") != policy["base_sha"]:
            raise ConductorError("First observed PR must be open on the exact approved base")
        _save(confirmation_path, confirmation)
    elif earlier != confirmation:
        raise ConductorError("Durable original PR confirmation differs")
    return observed["number"]


def _gate_predicate(policy: dict, bundle: dict, pr: int, archive_sha: str,
                    runner_pins: dict) -> dict:
    profile = policy["gate_profile"]
    if not isinstance(profile, dict) or set(profile) != gate_policy.PROFILE_FIELDS:
        raise ConductorError("Owner gate profile differs")
    if (runner_pins.get("candidate_history_sha256") is None
            or any(runner_pins.get(name) != profile[name] for name in
                   ("runner_identity", "runner_version", "firewall_policy_sha256",
                    "network_probe_sha256", "trusted_entrypoint_sha256",
                    "attestation_signer_sha256"))
            or any(runner_pins.get(name) != policy["images"][name] for name in
                   ("runner_image_id", "postgres_image_id", "firewall_image_id"))):
        raise ConductorError("Publisher runner pins differ from one-shot gate policy")
    return {"bundle_id": bundle["bundle_id"], "pr_number": pr,
            "target_ref": bundle["target_ref"], "target_base": bundle["base_commit"],
            "candidate_commit": bundle["candidate_commit"],
            "candidate_tree": bundle["candidate_tree"],
            "candidate_archive_sha256": archive_sha,
            "candidate_history_sha256": runner_pins["candidate_history_sha256"],
            "execution_host": policy["execution_host"],
            **profile, **policy["images"]}


def _gate_input(client: Client, checkout: Path, policy: dict, trust: dict, state: Path,
                permit_sha: str, members: list[dict], frozen: list[dict], bundle: dict,
                pr: int, predicate: dict, conductor_intent_sha: str) -> Path:
    output = state / "gate-input.json"
    if output.exists():
        envelope = gate_policy.parse(gate_policy.private(output))
        observed = gate_policy.verify(envelope, gate_policy.INTEGRATION, trust["integration"])
        if (observed.get("policy_sha256") != permit_sha
                or observed.get("conductor_intent_sha256") != conductor_intent_sha
                or observed.get("candidate_commit") != bundle["candidate_commit"]
                or observed.get("pr_number") != pr):
            raise ConductorError("Retained signed gate input differs")
        return output
    signed_reviews = []
    assignments = []
    for task in policy["tasks"]:
        path = state / (gate_policy.digest(task["task_id"].encode()) + ".review.json")
        envelope = gate_policy.parse(gate_policy.private(path))
        signer = [item for item in trust["reviewers"] if item["principal"] == envelope.get("principal")]
        if len(signer) != 1:
            raise ConductorError("Gate review signer is not independently trusted")
        gate_policy.verify(envelope, gate_policy.REVIEW, signer[0])
        signed_reviews.append(envelope)
        assignments.append({name: task[name] for name in
                            ("task_id", "assignment_id", "worker_id", "task_branch", "brief_sha256")})
    mapped = gate_members(members, assignments, signed_reviews)
    for item in mapped:
        view = client.task_workflow(policy["project_id"], item["task_id"])
        token = Store.workflow_token(view["task"])
        if view.get("token") != token.to_dict() or token.place != Place.INTEGRATING:
            raise ConductorError("Frozen focused API evidence is not current")
        focused = {}
        for stage, result_stage in (("unit", ValidationStage.UNIT_TESTS),
                                    ("long", ValidationStage.LONG_TESTS)):
            reference = gate_policy.fields(_read(
                state / (gate_policy.digest(item["task_id"].encode())
                         + "." + stage + ".focused.reference.json")),
                {"path", "sha256"})
            path = Path(reference["path"])
            gate_policy.outside(path, checkout)
            if gate_policy.digest(gate_policy.private(path)) != reference["sha256"]:
                raise ConductorError("Frozen focused receipt bytes differ")
            uri = str(path) + "#sha256=" + reference["sha256"]
            results = [entry for entry in token.evidence
                       if entry.stage == result_stage and entry.state == ResultState.PASSED
                       and _result_current(token, entry)
                       and entry.producer == trust["validation"]["principal"]
                       and uri in entry.artifacts]
            if len(results) != 1:
                raise ConductorError("Actual focused receipt is absent from frozen API validation")
            focused[stage] = reference
        item["focused_results"] = focused
    payload = frozen_gate_input(
        client, policy["project_id"], members, frozen, mapped,
        policy_sha256=permit_sha, target_ref=bundle["target_ref"],
        base_sha=bundle["base_commit"], prepared_manifest_sha256=bundle["manifest_sha256"],
        candidate_commit=bundle["candidate_commit"], candidate_tree=bundle["candidate_tree"],
        candidate_archive_sha256=predicate["candidate_archive_sha256"],
        candidate_history_sha256=predicate["candidate_history_sha256"],
        bundle_id=bundle["bundle_id"], pr_number=pr,
        pr_head_sha=bundle["candidate_commit"], pr_base_sha=bundle["base_commit"],
        pr_source_ref=policy["delivery"]["pr_source_ref"],
        frozen_at=datetime.now(timezone.utc).isoformat(),
        conductor_intent_sha256=conductor_intent_sha,
        key=_key(Path(trust["integration"]["key_path"])),
        key_id=trust["integration"]["key_id"])
    signer = trust["integration"]
    material = _key(Path(signer["key_path"]))
    envelope = {"schema": gate_policy.INTEGRATION, "key_id": signer["key_id"],
                "principal": signer["principal"], "payload": payload,
                "signature": hmac.new(material, gate_policy.INTEGRATION.encode() + b"\0"
                                      + gate_policy.canonical(payload), hashlib.sha256).hexdigest()}
    _save(output, envelope)
    return output


def _gate_stage(checkout: Path, policy: dict, trust: dict, state: Path,
                permit_path: Path, permit_sha: str, trust_path: Path, trust_sha: str,
                input_path: Path, predicate_path: Path, publisher_keys: dict,
                active_key_id: str, runner_pins: dict) -> Path | None:
    from isolated_full_test_gate import execute_policy
    from trusted_gate_attestation import verify_attestation
    predicate = gate_policy.parse(gate_policy.private(predicate_path))
    output_dir = state / "gate-runs"
    if not output_dir.exists():
        output_dir.mkdir(mode=0o700)
    _checked_dir(output_dir)
    intent_path = state / "full-gate.intent.json"
    intent = {"schema": "skybuild.full-gate-intent.v1", "permit_id": policy["permit_id"],
              "input_sha256": gate_policy.digest(gate_policy.private(input_path)),
              "predicate_sha256": gate_policy.digest(gate_policy.private(predicate_path)),
              "attestation_key_id": active_key_id}
    if not intent_path.exists():
        _save(intent_path, intent)
        result = execute_policy(checkout, predicate_path, permit_path, permit_sha,
                                trust_path, trust_sha, input_path,
                                publisher_keys[active_key_id], active_key_id, output_dir)
        if (result.get("exit_code") != 0 or result.get("cleanup_confirmed") is not True
                or result.get("failure") is not None or not result.get("attestation")):
            raise ConductorError("Isolated full gate failed; one-shot permit remains consumed")
    elif _read(intent_path) != intent:
        raise ConductorError("Full-gate intent differs from approved predicate")
    paths = sorted(output_dir.glob("run-*/attestation.json"))
    if len(paths) != 1:
        return None
    receipt = gate_policy.parse(gate_policy.private(paths[0]))
    expected = {"bundle_id": predicate["bundle_id"], "pr_number": predicate["pr_number"],
                "target_ref": predicate["target_ref"], "target_base": predicate["target_base"],
                "candidate_commit": predicate["candidate_commit"],
                "candidate_tree": predicate["candidate_tree"],
                "candidate_archive_sha256": predicate["candidate_archive_sha256"],
                "gate_argv": predicate["gate_argv"],
                "gate_command_sha256": predicate["gate_command_sha256"],
                "gate_policy_sha256": predicate["gate_policy_sha256"],
                **runner_pins}
    verify_attestation(receipt, trusted_keys=publisher_keys,
                       expected_predicate=expected, expected_key_id=active_key_id)
    return paths[0]


def _publisher_stage(checkout: Path, policy: dict, state: Path, bundle: dict,
                     prepared: Path, attestation: Path, pr: int) -> dict | None:
    from bundle_publisher import reconcile_bundle
    intent_path = state / "publisher-call.intent.json"
    intent = {"schema": "skybuild.publisher-call-intent.v1", "permit_id": policy["permit_id"],
              "bundle_id": bundle["bundle_id"], "candidate_commit": bundle["candidate_commit"],
              "target_ref": bundle["target_ref"], "base_sha": bundle["base_commit"],
              "pr": pr, "attestation_sha256": gate_policy.digest(gate_policy.private(attestation)),
              "publisher_trust_sha256": policy["delivery"]["publisher_trust_sha256"]}
    publication_intents = state / "publication-intents"
    if not publication_intents.exists():
        publication_intents.mkdir(mode=0o700)
    _checked_dir(publication_intents)
    if not intent_path.exists():
        _save(intent_path, intent)
        environment = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
        environment.update(GIT_TERMINAL_PROMPT="0", GH_PROMPT_DISABLED="1",
                           PYTHONPATH=os.pathsep.join((str(checkout / "src"),
                                                       str(checkout / "scripts"), str(checkout))),
                           UV_PROJECT_ENVIRONMENT=str(checkout / ".venv"))
        argv = [str(checkout / ".venv/bin/python"), "-P",
                str(checkout / "scripts/bundle_publisher.py"), "publish",
                "--checkout", str(checkout), "--prepared", str(prepared),
                "--attestation", str(attestation), "--pr", str(pr),
                "--trust-config", str(state / "publisher-trust.json"),
                "--trust-config-sha256", policy["delivery"]["publisher_trust_sha256"],
                "--intent-dir", str(publication_intents)]
        subprocess.run(argv, cwd=checkout, env=environment, capture_output=True,
                       timeout=180, check=False)
    elif _read(intent_path) != intent:
        raise ConductorError("Publisher call intent differs")
    observed = reconcile_bundle(checkout=checkout, intent_dir=publication_intents,
                                remote="origin", target_ref=bundle["target_ref"], pr=pr)
    if (observed.get("state") != "confirmed" or observed.get("bundle_id") != bundle["bundle_id"]
            or observed.get("candidate") != bundle["candidate_commit"]
            or observed.get("tree") != bundle["candidate_tree"]
            or observed.get("expected_base") != bundle["base_commit"]
            or observed.get("pr") != pr):
        return None
    _existing_or_save(state / "publisher.confirmed.json", observed)
    return observed


def _accept_both(client: Client, checkout: Path, policy: dict, trust: dict,
                 state: Path, bundle: dict, frozen: list[dict],
                 attestation: Path, pr: int) -> bool:
    signer = trust["integration"]
    key = _key(Path(signer["key_path"]))
    for task, original in zip(policy["tasks"], frozen, strict=True):
        task_id = task["task_id"]
        suffix = gate_policy.digest(task_id.encode())
        packet_path = state / (suffix + ".accept.packet.json")
        if not packet_path.exists():
            view = client.task_workflow(policy["project_id"], task_id)
            operation_id = "auto-accept-" + gate_policy.digest(
                (policy["permit_id"] + ":" + task_id).encode())[:32]
            packet = accept(view, bundle, original, checkout=checkout,
                            prepared=state / "prepared", attestation_path=attestation,
                            pr=pr, trust_config=state / "publisher-trust.json",
                            trust_sha256=policy["delivery"]["publisher_trust_sha256"],
                            intent_dir=state / "publication-intents",
                            operation_id=operation_id, producer=signer["principal"],
                            key_id=signer["key_id"], key=key,
                            source_head=policy["delivery"]["integration_source_sha"])
            _save(packet_path, packet)
        _source(checkout, policy["delivery"]["integration_source_sha"])
        result = submit(client, policy["project_id"], task_id, packet_path,
                        state / (suffix + ".accept.intent.json"))
        if not result.get("confirmed"):
            return False
        view = client.task_workflow(policy["project_id"], task_id)
        token = Store.workflow_token(view["task"])
        if (view.get("token") != token.to_dict() or token.place != Place.DONE
                or token.source_head != original["binding"]["source_head"]
                or token.bundle_id != bundle["bundle_id"]):
            raise ConductorError("Accepted task no longer binds exact published worker head")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--permit", type=Path, required=True)
    parser.add_argument("--permit-sha256", required=True)
    parser.add_argument("--trust", type=Path, required=True)
    parser.add_argument("--trust-sha256", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--watch", action="store_true",
                        help="Resume pending evidence automatically until delivery or permit expiry")
    args = parser.parse_args(argv)
    if args.watch:
        invocation = [sys.executable, "-P", str(Path(__file__).resolve()),
                      "--checkout", str(args.checkout), "--permit", str(args.permit),
                      "--permit-sha256", args.permit_sha256, "--trust", str(args.trust),
                      "--trust-sha256", args.trust_sha256,
                      "--token-file", str(args.token_file)]
        while True:
            try:
                observed = subprocess.run(invocation, cwd=args.checkout, text=True,
                                          capture_output=True, timeout=5400)
            except subprocess.TimeoutExpired:
                print(json.dumps({"state": "conductor_invocation_timeout_held"}),
                      file=sys.stderr)
                return 2
            if len(observed.stdout) > 8192 or len(observed.stderr) > 8192:
                raise ConductorError("Conductor watch child exceeded bounded output")
            if observed.stdout:
                print(observed.stdout.strip(), flush=True)
            if observed.stderr:
                print(observed.stderr.strip(), file=sys.stderr, flush=True)
            if observed.returncode != 3:
                return observed.returncode
            time.sleep(20)
    try:
        policy, trust, conductor_intent_sha = authorize(
            args.checkout, args.permit, args.permit_sha256, args.trust, args.trust_sha256)
        state = Path(policy["delivery"]["state_dir"])
        from bundle_publisher import _candidate_archive_sha256, _read_trust_config
        publisher_config = state / "publisher-trust.json"
        publisher_keys, active_key_id, publisher_source, runner_pins, read_client = _read_trust_config(
            publisher_config, policy["delivery"]["publisher_trust_sha256"], args.checkout)
        if publisher_source != policy["delivery"]["publisher_source_sha"]:
            raise ConductorError("Publisher runtime source differs from approval")
        endpoint = _private_endpoint(read_client["base_url"], _resolved_addresses)
        if ca_file_sha256(Path(read_client["ca_file"])) != read_client["ca_sha256"]:
            raise ConductorError("Production API CA differs from publisher pin")
        with Client(endpoint, _token_from_file(args.token_file), retries=0, timeout=10,
                    trust_env=False, ca_file=Path(read_client["ca_file"]),
                    expected_ca_sha256=read_client["ca_sha256"]) as client:
            identity = client.whoami()
            if (identity.get("principal_id") != trust["integration"]["principal"]
                    or identity.get("is_admin") is not False):
                raise ConductorError("Conductor API principal differs from dedicated integration producer")
            if not (state / "reviewed-members.json").exists():
                for task in policy["tasks"]:
                    if not _role_stage(args.checkout, policy, args.permit, args.permit_sha256,
                                       args.trust, args.trust_sha256, task["task_id"], "static"):
                        print(json.dumps({"state": "awaiting_worker_submission",
                                          "task_id": task["task_id"]}))
                        return 3
                for position, task in enumerate(policy["tasks"]):
                    for stage in ("unit", "long"):
                        receipt = _focused_stage(
                            client, args.checkout, policy, trust, state, task, position,
                            stage, args.permit, args.permit_sha256, args.trust,
                            args.trust_sha256, conductor_intent_sha, runner_pins)
                        if receipt is None:
                            print(json.dumps({"state": "unknown_focused_gate_hold_get_only",
                                              "task_id": task["task_id"], "stage": stage}))
                            return 3
                        if not _role_stage(args.checkout, policy, args.permit,
                                           args.permit_sha256, args.trust, args.trust_sha256,
                                           task["task_id"], "focused", stage=stage):
                            print(json.dumps({"state": "unknown_focused_result_hold_get_only",
                                              "task_id": task["task_id"], "stage": stage}))
                            return 3
                for task in policy["tasks"]:
                    if not _role_stage(args.checkout, policy, args.permit, args.permit_sha256,
                                       args.trust, args.trust_sha256, task["task_id"], "review"):
                        print(json.dumps({"state": "awaiting_signed_focused_validation",
                                          "task_id": task["task_id"]}))
                        return 3
            prepared = _prepared(client, policy, trust, state, args.checkout, args.permit)
            if prepared is None:
                print(json.dumps({"state": "awaiting_independent_worker_reviews"}))
                return 3
            report, members = prepared
            frozen = _freeze_both(client, policy, trust, state, args.checkout, members)
            if frozen is None:
                print(json.dumps({"state": "unknown_freeze_hold_get_only"}))
                return 3
            bundle = _bundle(args.checkout, state / "prepared")
            if (state / "publisher.confirmed.json").exists():
                retained = _read(state / "bundle-pr.confirmed.json")
                pr = retained["pr"]
            else:
                pr = _pr_stage(args.checkout, policy, state, report, bundle)
            if pr is None:
                print(json.dumps({"state": "unknown_pr_hold_get_only"}))
                return 3
            predicate = _gate_predicate(policy, bundle, pr,
                                        _candidate_archive_sha256(args.checkout, bundle["candidate_commit"]),
                                        runner_pins)
            predicate_path = state / "expected-gate-predicate.json"
            _existing_or_save(predicate_path, predicate)
            input_path = _gate_input(client, args.checkout, policy, trust, state,
                                     args.permit_sha256, members, frozen, bundle,
                                     pr, predicate, conductor_intent_sha)
            candidate = Path(report["candidate"])
            if (candidate != state / "prepared" / "candidate"
                    or _command(["git", "rev-parse", "HEAD"], candidate)
                    != bundle["candidate_commit"]):
                raise ConductorError("Prepared gate checkout differs from exact bundle candidate")
            attestation = _gate_stage(candidate, policy, trust, state,
                                      args.permit, args.permit_sha256, args.trust,
                                      args.trust_sha256, input_path, predicate_path,
                                      publisher_keys, active_key_id, runner_pins)
            if attestation is None:
                print(json.dumps({"state": "unknown_gate_hold_get_only"}))
                return 3
            publication = _publisher_stage(args.checkout, policy, state, bundle,
                                           state / "prepared", attestation, pr)
            if publication is None:
                print(json.dumps({"state": "unknown_publication_hold_get_only"}))
                return 3
            if not _accept_both(client, args.checkout, policy, trust, state, bundle,
                                frozen, attestation, pr):
                print(json.dumps({"state": "unknown_accept_hold_get_only"}))
                return 3
            print(json.dumps({"state": "delivered", "bundle_id": bundle["bundle_id"],
                              "candidate": report["candidate_head"], "pr": pr,
                              "attestation": str(attestation),
                              "observed_target": publication["observed_target"],
                              "conductor_intent_sha256": conductor_intent_sha}, sort_keys=True))
            return 0
    except (OSError, ValueError, KeyError, TypeError, gate_policy.PolicyError,
            prepare_bundle.PreparationError) as error:
        print(json.dumps({"ok": False, "error": type(error).__name__}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
