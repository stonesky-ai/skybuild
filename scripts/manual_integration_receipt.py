#!/usr/bin/env python3
"""Check real Git/GitHub evidence, then prepare or submit a manual owner statement.

This bounded operator tool never publishes, runs a gate, launches a worker or
qualifies an automatic publisher. External observations remain owner attested.
"""
import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

from _repo_guard import verify_skybuild, RepoGuardError
from skybuild.client import Client, ClientError
from skybuild.completion import generation
from skybuild.contracts import DomainError
from skybuild.fleet_preflight import _token_from_file, _resolved_addresses
from skybuild.manual_dispatch import _private_endpoint
from skybuild.manual_integration import (SCHEMA, binding, canonical, digest, validation_digest,
                                         validate_evidence, DEFAULT_GATE_COMMAND_SHA256)
from skybuild.integration_workflow import satisfactory_validation
from skybuild.store import Store


class ReceiptError(ValueError):
    pass


def require(condition):
    if not condition:
        raise ReceiptError("Manual integration evidence did not pass operator checks")


def read_bytes(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        require(stat.S_ISREG(info.st_mode) and 0 < info.st_size <= 262144)
        raw = os.read(descriptor, 262145)
        require(len(raw) <= 262144)
        return raw
    finally:
        os.close(descriptor)


def read_json(path):
    return json.loads(read_bytes(path))


def file_digest(path):
    return hashlib.sha256(read_bytes(path)).hexdigest()


def command(checkout, *argv):
    require(not any(name.startswith("GIT_") and name != "GIT_PAGER" for name in os.environ))
    environment = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    environment["GIT_TERMINAL_PROMPT"] = "0"
    result = subprocess.run(list(argv), cwd=checkout, capture_output=True, text=True,
                            timeout=30, env=environment)
    require(result.returncode == 0 and len(result.stdout.encode()) <= 262144)
    return result.stdout.strip()


def git(checkout, *args):
    return command(checkout, "git", *args)


def remote_head(checkout, ref):
    fields = git(checkout, "ls-remote", "--exit-code", "origin", ref).split()
    require(len(fields) == 2 and fields[1] == ref)
    return fields[0]


def ancestor(checkout, first, last):
    git(checkout, "merge-base", "--is-ancestor", first, last)


def prepared_bundle(checkout, prepared):
    """Recheck retained preparation bytes and Git objects, not its success flag."""
    report, owner = read_json(prepared / "report.json"), read_json(prepared / "inputs.json")
    inputs = owner["inputs"]
    fingerprint = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    require(owner["fingerprint"] == fingerprint == report["fingerprint"]
            and inputs == report["inputs"] and report["schema"] == "skybuild.bundle-preparation.v1"
            and inputs["schema"] == "skybuild.bundle-input.v1" and report["ok"] is True)
    require(file_digest(Path(inputs["policy"]["path"])) == inputs["policy"]["sha256"])
    members = []
    for item in inputs["members"]:
        review = read_bytes(Path(item["review"]["path"]))
        require(hashlib.sha256(review).hexdigest() == item["review"]["sha256"])
        # Preparation already requires exact-head review. Verify it still exists
        # and bind its bytes, rather than claim that text parsing proves quality.
        require(item["sha"].encode() in review)
        require(remote_head(checkout, item["ref"]) == item["sha"])
        ancestor(checkout, item["sha"], report["candidate_head"])
        members.append({"task_id": item["task_id"], "source_head": item["sha"],
                        "source_branch": item["ref"], "reviewer": item["reviewer"],
                        "review_sha256": item["review"]["sha256"]})
    require([(item["task_id"], item["head"]) for item in report["included"]]
            == [(item["task_id"], item["source_head"]) for item in members])
    require(git(checkout, "rev-parse", report["candidate_head"] + "^{tree}") == report["candidate_tree"])
    ancestor(checkout, inputs["target"]["sha"], report["candidate_head"])
    return {"bundle_id": "bundle-" + fingerprint[:24], "manifest_sha256": fingerprint,
            "policy_sha256": inputs["policy"]["sha256"], "target_ref": inputs["target"]["ref"],
            "base_commit": inputs["target"]["sha"], "candidate_commit": report["candidate_head"],
            "candidate_tree": report["candidate_tree"], "members": members}


def packet(view, attestor, event, operation_id, bundle):
    token = Store.workflow_token(view["task"])
    require(token.place.value == ("validating" if event == "freeze" else "integrating")
            and token.pending_action is None and not token.superseded and satisfactory_validation(token))
    return {"schema": SCHEMA, "event": event, "operation_id": operation_id,
            "expected_revision": token.revision, "attestor": attestor,
            "authority": "manual_owner_attestation", "binding": binding(token), "bundle": bundle,
            "validation_sha256": validation_digest(token), "freeze_sha256": None,
            "gate": None, "publication": None, "completion": None}


def freeze_packet(checkout, view, attestor, operation_id, prepared):
    bundle = prepared_bundle(checkout, prepared)
    require(remote_head(checkout, bundle["target_ref"]) == bundle["base_commit"])
    result = packet(view, attestor, "freeze", operation_id, bundle)
    validate_evidence(result, event="freeze", operation_id=operation_id,
                      expected_revision=result["expected_revision"], attestor=attestor)
    return result


def accepted_packet(checkout, view, attestor, operation_id, frozen, integration_path):
    """Observe the merged PR and published objects before attesting acceptance."""
    validate_evidence(frozen, event="freeze", operation_id=frozen["operation_id"],
                      expected_revision=frozen["expected_revision"], attestor=frozen["attestor"])
    bundle = frozen["bundle"]
    result = packet(view, attestor, "accept", operation_id, deepcopy(bundle))
    require(result["binding"] == frozen["binding"]
            and result["validation_sha256"] == frozen["validation_sha256"])
    integrated = read_json(integration_path)
    require(integrated["ok"] is True and integrated["merged"] is True
            and integrated["head"] == bundle["candidate_commit"]
            and integrated["base"] == bundle["base_commit"]
            and integrated["expected_base"] == bundle["base_commit"]
            and integrated["candidate_tree"] == bundle["candidate_tree"]
            and integrated["atomic_expected_base"] is False)
    artifact_path = Path(integrated["gate_artifact"])
    durable = read_json(artifact_path)
    summary = integrated["gate"]
    require(durable["schema"] == "skybuild.gate-run.v1" and durable["ok"] is True
            and summary["ok"] is True and summary["cleaned_up"] is True
            and summary["run_id"] == durable["run_id"]
            and summary["artifact"] == str(artifact_path)
            and durable["head"] == integrated["candidate_head"]
            and durable["tree"] == integrated["candidate_tree"]
            and durable["command_sha256"] == DEFAULT_GATE_COMMAND_SHA256
            and durable["phase"] == "terminal" and durable["status"] == "passed"
            and durable["cleanup"] == "confirmed" and type(durable["exit_code"]) is int
            and durable["exit_code"] == 0)
    pr = integrated["pr"]
    require(type(pr) is int and 0 < pr < 2**31)
    observed = json.loads(command(checkout, "gh", "pr", "view", str(pr), "--repo", "stonesky-ai/skybuild",
        "--json", "state,headRefOid,baseRefName,mergeCommit"))
    commit = integrated["published_commit"]
    require(observed["state"] == "MERGED" and observed["headRefOid"] == bundle["candidate_commit"]
            and "refs/heads/" + observed["baseRefName"] == bundle["target_ref"]
            and observed["mergeCommit"]["oid"] == commit)
    target = remote_head(checkout, bundle["target_ref"])
    git(checkout, "fetch", "--no-tags", "origin", bundle["target_ref"])
    require(git(checkout, "rev-parse", "FETCH_HEAD") == target)
    require(git(checkout, "rev-parse", commit + "^{tree}") == bundle["candidate_tree"])
    ancestor(checkout, commit, target)
    ancestor(checkout, bundle["candidate_commit"], commit)
    for member in bundle["members"]:
        ancestor(checkout, member["source_head"], commit)
    observation = {"pr": observed, "target": target, "integration_sha256": file_digest(integration_path)}
    publication = {"repository": "stonesky-ai/skybuild", "pr": pr,
        "head_commit": bundle["candidate_commit"], "base_ref": bundle["target_ref"],
        "base_commit": bundle["base_commit"], "target_commit": commit,
        "target_tree": bundle["candidate_tree"], "observed_target_commit": target,
        "task_head": result["binding"]["source_head"], "observation_sha256": digest(observation)}
    gate = {name: durable[name] for name in ("run_id", "head", "tree", "command_sha256", "phase", "status", "cleanup", "exit_code")}
    require(git(checkout, "rev-parse", durable["head"] + "^{tree}") == bundle["candidate_tree"])
    ancestor(checkout, bundle["candidate_commit"], durable["head"])
    gate["artifact_sha256"] = file_digest(artifact_path)
    task, token = view["task"], Store.workflow_token(view["task"])
    own = next(item for item in bundle["members"] if item["task_id"] == token.task_id)
    evidence_ref = "manual-integration:" + publication["observation_sha256"]
    result.update(freeze_sha256=digest(frozen), gate=gate, publication=publication,
        completion={"reason": "Owner attests actual checks, independent review and confirmed bundle publication",
            "generation": generation(task), "source_head": token.source_head,
            "author": token.responsible, "policy_ref": token.policy_version,
            "acceptance": [{"criterion": value, "evidence_ref": evidence_ref} for value in task["acceptance_criteria"]],
            "checks": [{"name": "required-validation", "source_head": token.source_head, "result": "passed",
                        "evidence_ref": "petri-validation:" + result["validation_sha256"]}],
            "review": {"reviewer": own["reviewer"], "session_ref": "manual-review:" + own["review_sha256"],
                       "source_head": token.source_head, "result": "passed", "unresolved_blocking_findings": 0,
                       "evidence_ref": "manual-review:" + own["review_sha256"]},
            "publication": {"source_head": token.source_head, "candidate_commit": gate["head"],
                "base_commit": bundle["base_commit"], "target_commit": commit, "target_ref": bundle["target_ref"],
                "result": "confirmed", "inclusion_evidence_ref": evidence_ref}})
    validate_evidence(result, event="accept", operation_id=operation_id,
                      expected_revision=result["expected_revision"], attestor=attestor)
    return result


def write_new(path, value):
    parent = path.parent
    info = parent.lstat()
    require(path.is_absolute() and parent == parent.resolve() and stat.S_ISDIR(info.st_mode)
            and info.st_uid == os.geteuid() and not info.st_mode & 0o077)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(canonical(value) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("url", "project", "task-id"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--ca-file", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("freeze", "accept", "unknown"):
        action = commands.add_parser(name)
        action.add_argument("--operation-id", required=True)
        action.add_argument("--output", type=Path, required=True)
        if name != "unknown":
            action.add_argument("--checkout", type=Path, required=True)
        if name == "freeze":
            action.add_argument("--prepared", type=Path, required=True)
        else:
            action.add_argument("--freeze-receipt", type=Path, required=True)
        if name == "accept":
            action.add_argument("--integration-artifact", type=Path, required=True)
        if name == "unknown":
            action.add_argument("--publication-operation", required=True)
    submit = commands.add_parser("submit")
    submit.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        endpoint = _private_endpoint(args.url, _resolved_addresses)
        with Client(endpoint, _token_from_file(args.token_file), ca_file=args.ca_file,
                    retries=0, timeout=5, trust_env=False) as client:
            identity = client.whoami()
            require(identity.get("is_admin") is True)
            attestor = identity["principal_id"]
            if args.command == "submit":
                evidence = read_json(args.receipt)
                require(evidence["binding"]["project_id"] == args.project
                        and evidence["binding"]["task_id"] == args.task_id)
                validate_evidence(evidence, event=evidence["event"], operation_id=evidence["operation_id"],
                                  expected_revision=evidence["expected_revision"], attestor=attestor)
                outcome = client.manual_integration(args.project, args.task_id, evidence["event"], evidence,
                    expected_revision=evidence["expected_revision"], idempotency_key=evidence["operation_id"])
                print(json.dumps({"recorded": True, "place": outcome["token"]["place"],
                                  "revision": outcome["task"]["revision"], "receipt_sha256": digest(evidence)}))
                return 0
            view = client.task_workflow(args.project, args.task_id)
            if args.command == "freeze":
                evidence = freeze_packet(verify_skybuild(args.checkout), view, attestor, args.operation_id, args.prepared)
            else:
                frozen = read_json(args.freeze_receipt)
                if args.command == "accept":
                    evidence = accepted_packet(verify_skybuild(args.checkout), view, attestor, args.operation_id,
                                               frozen, args.integration_artifact)
                else:
                    validate_evidence(frozen, event="freeze", operation_id=frozen["operation_id"],
                                      expected_revision=frozen["expected_revision"], attestor=frozen["attestor"])
                    evidence = packet(view, attestor, "integration_progress", args.operation_id, frozen["bundle"])
                    evidence.update(freeze_sha256=digest(frozen),
                                    publication={"outcome": "unknown", "operation_ref": args.publication_operation})
                    validate_evidence(evidence, event="integration_progress", operation_id=args.operation_id,
                                      expected_revision=evidence["expected_revision"], attestor=attestor)
            write_new(args.output, evidence)
            print(json.dumps({"prepared": True, "receipt_sha256": digest(evidence), "submitted": False}))
            return 0
    except (ReceiptError, RepoGuardError, DomainError, ClientError, OSError, ValueError, TypeError, KeyError,
            StopIteration, subprocess.SubprocessError):
        print("Manual integration unconfirmed; preserve original artifacts and operation ID.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
