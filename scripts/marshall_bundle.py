#!/usr/bin/env python3
"""Plan reviewed task bundles against REST authority; optionally prepare/gate the next one."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

# Select this checkout's source and helpers, including when safe-path mode is enabled.
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path[:0] = [str(SCRIPT_DIR.parent / "src"), str(SCRIPT_DIR)]

from _repo_guard import verify_skybuild
import prepare_bundle as preparation
from skybuild.bundling import BundleCandidate, BundlePlanningError, plan_bundles
from skybuild.client import Client, ClientError
from skybuild.fleet_preflight import _token_from_file, _resolved_addresses
from skybuild.manual_dispatch import _private_endpoint


_SAFE_ADMISSION_DIAGNOSTICS = frozenset({
    'Available memory is below the required 6 GiB reserve',
    'Available memory cannot be measured',
    'Remote refs moved or are missing; freeze new inputs',
})


def safe_admission_diagnostic(error):
    """Expose only exact static admission messages, never command stderr."""
    detail = str(error)
    return detail if detail in _SAFE_ADMISSION_DIAGNOSTICS else None


def marshall(checkout, catalog, output, client, *, project, principal, prepare_next=False, gate_next=False, gate_runner=None):
    """Read explicit reviewed heads once. Never claim tasks, launch workers, or publish."""
    preparation._git_environment()
    root = verify_skybuild(checkout)
    preparation._clean(root)
    if catalog.stat().st_size > 262144:
        raise BundlePlanningError("Catalog exceeds 256 KiB")
    supplied = json.loads(catalog.read_text())
    required = {"schema", "target_ref", "base_sha", "policy_evidence", "members"}
    optional = {"in_flight", "already_bundled", "ignored_paths"}
    if (not isinstance(supplied, dict) or supplied.get("schema") != "skybuild.marshall-input.v1"
            or not required <= supplied.keys() or supplied.keys() - required - optional):
        raise BundlePlanningError("Unknown catalog contract")
    members = supplied["members"]
    if not isinstance(members, list) or not 1 <= len(members) <= 100:
        raise BundlePlanningError("Supply 1..100 explicit reviewed task heads")
    for field in optional:
        values = supplied.get(field, [])
        if not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values):
            raise BundlePlanningError(f"{field} must contain nonempty text")
    if any(not isinstance(supplied[field], str) or not supplied[field] for field in ("target_ref", "base_sha", "policy_evidence")):
        raise BundlePlanningError("Catalog target, base and policy must be text")
    identity = client.whoami()
    if not isinstance(identity, dict) or identity.get("principal_id") != principal:
        raise BundlePlanningError("API principal differs from the requested identity")
    # The API enforces project read scope on every task read, including for scoped owners.
    excluded = set(supplied.get("in_flight", [])) | set(supplied.get("already_bundled", []))
    selected, snapshots, skipped, seen = [], {}, [], set()
    for member in members:
        if (not isinstance(member, dict) or set(member) != {"task_id", "ref", "head_sha", "review"}
                or not isinstance(member.get("task_id"), str) or not member["task_id"].strip()
                or not isinstance(member.get("ref"), str) or not member["ref"].startswith("refs/heads/task/")
                or not isinstance(member.get("review"), dict)
                or set(member["review"]) != {"verdict", "head_sha", "reviewer", "evidence"}):
            raise BundlePlanningError("Every member requires a task ID")
        task_id = member["task_id"]
        if task_id in seen:
            raise BundlePlanningError("Duplicate catalog task ID")
        seen.add(task_id)
        if task_id in excluded:
            skipped.append({"task_id": task_id, "reason": "already bundled or in flight"})
            continue
        task = client.get_task(project, task_id)
        if (not isinstance(task, dict) or task.get("task_id") != task_id
                or type(task.get("revision")) is not int or task["revision"] < 1
                or not isinstance(task.get("dependencies"), list)
                or any(not isinstance(value, str) or not value for value in task["dependencies"])):
            raise BundlePlanningError("Invalid API task snapshot")
        if task.get("status") not in ("ready", "in-progress"):
            skipped.append({"task_id": task_id, "reason": "API task is not eligible"})
            continue
        snapshots[task_id] = {key: task.get(key) for key in ("task_id", "revision", "status", "phase", "dependencies")}
        selected.append(member)
    if output.exists() or output.is_symlink():
        raise BundlePlanningError("Use a new output directory; preserve prior evidence")
    output = output.resolve()
    worktrees = [Path(line[9:]).resolve() for line in preparation.git(root, "worktree", "list", "--porcelain").splitlines()
                 if line.startswith("worktree ")]
    if any(output.is_relative_to(path) or path.is_relative_to(output) for path in worktrees):
        raise BundlePlanningError("Output must be outside all existing worktrees")
    output.mkdir(mode=0o700)
    report = {"schema": "skybuild.marshall-report.v1", "project_id": project,
              "principal_id": principal, "snapshots": snapshots, "skipped": skipped,
              "bundles": [], "prepared": None, "gate": None, "gate_passed": False, "published": False,
              "next_action": "Await eligible reviewed task branches"}
    def save():
        preparation._write(output / "report.json", report)
    save()
    try:
        normalized = []
        # Reuse preparation's exact-head/evidence validator in bounded chunks.
        for start in range(0, len(selected), 20):
            chunk = selected[start:start + 20]
            manifest = {"schema": "skybuild.bundle-input.v1", "target_ref": supplied["target_ref"],
                        "base_sha": supplied["base_sha"],
                        "policy_evidence": str((catalog.parent / supplied["policy_evidence"]).resolve()),
                        "members": []}
            for member in chunk:
                review = member.get("review")
                if not isinstance(review, dict) or not isinstance(review.get("evidence"), str):
                    raise BundlePlanningError("Review evidence is required")
                manifest["members"].append({**member, "review": {**review,
                    "evidence": str((catalog.parent / review["evidence"]).resolve())}})
            path = output / f"validated-{start // 20 + 1}.json"
            preparation._write(path, manifest)
            frozen = preparation.frozen_inputs(root, path)
            normalized.extend(frozen["members"])
        if not selected:
            return report
        frozen["members"] = normalized
        # Preserve the exact validated evidence bytes, not mutable caller paths.
        evidence_dir = output / "evidence"
        evidence_dir.mkdir(mode=0o700)
        for index, item in enumerate([frozen["policy"], *(member["review"] for member in normalized)]):
            content = Path(item["path"]).read_bytes()
            if hashlib.sha256(content).hexdigest() != item["sha256"]:
                raise BundlePlanningError("Evidence changed after validation")
            destination = evidence_dir / f"{index:03}.txt"
            destination.write_bytes(content)
            destination.chmod(0o400)
            item["path"] = str(destination)
        report["frozen_inputs"] = frozen
        def check_evidence():
            for item in [frozen["policy"], *(member["review"] for member in normalized)]:
                if hashlib.sha256(Path(item["path"]).read_bytes()).hexdigest() != item["sha256"]:
                    raise BundlePlanningError("Frozen evidence changed")
        preparation.check_refs(root, frozen)
        preparation._reserve()
        preparation.git(root, "fetch", "--no-tags", "--no-write-fetch-head", "--refmap=", "origin",
                        frozen["target"]["ref"], *(member["ref"] for member in normalized))
        preparation.check_refs(root, frozen)
        candidates = []
        for member in normalized:
            task = snapshots[member["task_id"]]
            paths = preparation.git(root, "diff", "--name-only", "-z", "--no-renames",
                                    frozen["target"]["sha"] + "..." + member["sha"]).split("\0")[:-1]
            candidates.append(BundleCandidate(task_id=member["task_id"], status=task["status"],
                ref=member["ref"], head_sha=member["sha"],
                base_sha=frozen["target"]["sha"], changed_paths=frozenset(paths),
                dependencies=frozenset(task["dependencies"]), review_head_sha=member["sha"],
                review_verdict="pass", reviewer=member["reviewer"], review_evidence=member["review"]["path"]))
        plan = plan_bundles(candidates, ignored_paths=supplied.get("ignored_paths", []))
        by_id = {member["task_id"]: member for member in normalized}
        for index, group in enumerate(plan.bundles, 1):
            manifest = {"schema": "skybuild.bundle-input.v1", "target_ref": frozen["target"]["ref"],
                        "base_sha": frozen["target"]["sha"], "policy_evidence": frozen["policy"]["path"], "members": []}
            for candidate in group.members:
                member = by_id[candidate.task_id]
                manifest["members"].append({"task_id": candidate.task_id, "ref": candidate.ref,
                    "head_sha": candidate.head_sha, "review": {"verdict": "pass", "head_sha": candidate.head_sha,
                        "reviewer": candidate.reviewer, "evidence": candidate.review_evidence}})
            path = output / f"bundle-{index:03}.json"
            preparation._write(path, manifest)
            report["bundles"].append({"manifest": str(path), "task_ids": [member.task_id for member in group.members],
                                      "shared_paths": list(group.shared_paths), "dependency_edges": list(group.dependency_edges)})
        report["next_action"] = "Prepare the next frozen bundle; later groups require a fresh base after publication"
        save()
        def check_snapshots():
            for task_id in report["bundles"][0]["task_ids"]:
                current = client.get_task(project, task_id)
                if (not isinstance(current, dict) or current.get("task_id") != task_id
                        or type(current.get("revision")) is not int
                        or current["revision"] != snapshots[task_id]["revision"]
                        or current.get("status") != snapshots[task_id]["status"]):
                    raise BundlePlanningError("API task changed after planning")
        if prepare_next or gate_next:
            check_snapshots()
            check_evidence()
            report["prepared"] = preparation.prepare(root, Path(report["bundles"][0]["manifest"]), output / "next")
            report["next_action"] = "Run the required combined gate and independent candidate review"
            save()
        if gate_next:
            candidate = Path(report["prepared"]["candidate"])
            run_id = "marshall-" + uuid4().hex
            artifact = output / "gate.json"
            report["gate_artifact"] = str(artifact)
            save()
            process = (gate_runner or subprocess.run)(["nice", "-n", "10", sys.executable, str(root / "scripts/disposable_pg_gate.py"),
                                      "--checkout", str(candidate), "--min-available-gib", "6",
                                      "--artifact", str(artifact), "--run-id", run_id,
                                      "--expected-head", report["prepared"]["candidate_head"],
                                      "--expected-tree", report["prepared"]["candidate_tree"]],
                                     cwd=candidate, text=True, capture_output=True, timeout=3660)
            gate = json.loads(process.stdout)
            if not isinstance(gate, dict):
                raise BundlePlanningError("Gate returned an invalid result")
            report["gate"] = gate
            if process.returncode or gate.get("ok") is not True or gate.get("cleaned_up") is not True:
                report["next_action"] = "Resolve failed gate; preserve this candidate and freeze new inputs"
            else:
                durable = json.loads(artifact.read_text())
                expected = {"schema": "skybuild.gate-run.v1", "run_id": run_id,
                            "checkout": str(candidate), "head": report["prepared"]["candidate_head"],
                            "tree": report["prepared"]["candidate_tree"], "phase": "terminal",
                            "status": "passed", "cleanup": "confirmed", "ok": True, "exit_code": 0}
                if not isinstance(durable, dict) or any(durable.get(key) != value for key, value in expected.items()):
                    raise BundlePlanningError("Gate artifact does not prove this candidate passed")
                preparation.check_refs(root, frozen)
                check_evidence()
                preparation._clean(candidate)
                if (preparation.git(candidate, "rev-parse", "HEAD") != report["prepared"]["candidate_head"]
                        or preparation.git(candidate, "rev-parse", "HEAD^{tree}") != report["prepared"]["candidate_tree"]):
                    raise BundlePlanningError("Gate changed the candidate head or tree")
                check_snapshots()
                report["gate_passed"] = True
                report["next_action"] = "Independent exact-candidate review, guarded publication and confirmed task inclusion"
            save()
        return report
    except Exception as error:
        report["error"] = type(error).__name__
        detail = safe_admission_diagnostic(error)
        if detail is not None:
            report['error_detail'] = detail
        report["next_action"] = "Resolve the failure; preserve evidence and use a new output"
        save()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("checkout", "catalog", "output", "token-file", "ca-file"):
        parser.add_argument("--" + name, type=Path, required=name != "ca-file")
    for name in ("url", "project", "principal"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--prepare-next", action="store_true")
    parser.add_argument("--gate-next", action="store_true")
    args = parser.parse_args()
    try:
        _private_endpoint(args.url, _resolved_addresses)
        with Client(args.url, _token_from_file(args.token_file), ca_file=args.ca_file, retries=0, trust_env=False) as client:
            report = marshall(args.checkout, args.catalog.resolve(), args.output, client, project=args.project,
                              principal=args.principal, prepare_next=args.prepare_next, gate_next=args.gate_next)
        print(json.dumps(report, sort_keys=True))
        return 1 if report.get("gate") and not report["gate_passed"] else 0
    except (OSError, ValueError, RuntimeError, ClientError, subprocess.SubprocessError) as error:
        failure = {"ok": False, "error": type(error).__name__, "next_action": "Inspect retained evidence and frozen inputs"}
        detail = safe_admission_diagnostic(error)
        if detail is not None:
            failure['error_detail'] = detail
        print(json.dumps(failure))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
