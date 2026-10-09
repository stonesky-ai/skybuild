#!/usr/bin/env python3
"""Prepare an explicitly reviewed, frozen Git bundle; never gate or publish it."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from _repo_guard import RepoGuardError, verify_skybuild
from disposable_pg_gate import available_memory_bytes
from _worktree_capacity import MAX_WORKTREES, WorktreeCapacityError, reserve_worktree_slots


class PreparationError(RuntimeError):
    """The supplied frozen inputs cannot produce a reusable candidate."""


MAX_BUNDLE_TASKS = 20
PREPARE_WORKTREE_RESERVE = 2


def _git_environment() -> dict[str, str]:
    # Reject before the shared repository guard, which uses inherited environment.
    # This also covers Git's expandable config/environment injection interfaces.
    # RTK sets GIT_PAGER; it cannot route the guard's nonpaged read commands.
    if any(name.startswith("GIT_") and name != "GIT_PAGER" for name in os.environ):
        raise PreparationError("Inherited GIT_* environment is unsupported; use a clean Git environment")
    clean = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    return dict(clean, GIT_TERMINAL_PROMPT="0")


def git(root: Path, *arguments: str) -> str:
    process = subprocess.run(["nice", "-n", "10", "git", *arguments], cwd=root,
                             text=True, capture_output=True, timeout=120,
                             env=_git_environment())
    if process.returncode:
        raise PreparationError(f"git {arguments[0]} failed: "
                               + (process.stdout + process.stderr)[-12000:])
    return process.stdout.rstrip("\n")


def _evidence(path: str, relative_to: Path, head: str | None = None) -> dict:
    source = (relative_to / path).resolve()
    if not source.is_file() or source.stat().st_size > 256 * 1024:
        raise PreparationError("Evidence must be a nonempty file of at most 256 KiB")
    content = source.read_bytes()
    if not content.strip() or (head and head.encode("ascii") not in content):
        raise PreparationError("Review evidence must contain the exact reviewed head")
    return {"path": str(source), "sha256": hashlib.sha256(content).hexdigest()}


def frozen_inputs(root: Path, manifest: Path) -> dict:
    if manifest.stat().st_size > 256 * 1024:
        raise PreparationError("Manifest is too large")
    supplied = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(supplied, dict) or supplied.get("schema") != "skybuild.bundle-input.v1":
        raise PreparationError("Unknown manifest schema")
    members = supplied.get("members")
    if not isinstance(members, list) or not members or len(members) > MAX_BUNDLE_TASKS:
        raise PreparationError(f"Supply 1..{MAX_BUNDLE_TASKS} explicit reviewed members")

    def revision(ref, sha):
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise PreparationError("Every revision must be an explicit full lowercase SHA")
        if not isinstance(ref, str) or not ref.startswith("refs/heads/"):
            raise PreparationError("Use explicit refs/heads references")
        git(root, "check-ref-format", ref)
        return {"ref": ref, "sha": sha}

    result = {"schema": supplied["schema"], "checkout": str(root),
              "target": revision(supplied["target_ref"], supplied["base_sha"]),
              "policy": _evidence(supplied["policy_evidence"], manifest.parent),
              "members": []}
    seen_tasks, seen_refs = set(), {result["target"]["ref"]}
    for member in members:
        if not isinstance(member, dict) or not isinstance(member.get("review"), dict):
            raise PreparationError("Each member needs an explicit review object")
        task_id = member["task_id"]
        review = member["review"]
        if (not isinstance(task_id, str) or not task_id.strip() or len(task_id) > 160
                or task_id in seen_tasks):
            raise PreparationError("Supply unique, nonempty task IDs")
        if (review.get("verdict") != "pass" or review.get("head_sha") != member["head_sha"]
                or not isinstance(review.get("reviewer"), str) or not review["reviewer"].strip()):
            raise PreparationError("Every member needs explicit exact-head passing review")
        item = revision(member["ref"], member["head_sha"])
        if item["ref"] in seen_refs:
            raise PreparationError("Duplicate member/target ref")
        item.update(task_id=task_id, reviewer=review["reviewer"],
                    review=_evidence(review["evidence"], manifest.parent, item["sha"]))
        result["members"].append(item)
        seen_tasks.add(task_id)
        seen_refs.add(item["ref"])
    return result


def check_refs(root: Path, inputs: dict) -> None:
    revisions = [inputs["target"], *inputs["members"]]
    refs = [item["ref"] for item in revisions]
    lines = git(root, "ls-remote", "--refs", "origin", *refs).splitlines()
    actual = dict(line.split()[::-1] for line in lines)
    if actual != {item["ref"]: item["sha"] for item in revisions}:
        raise PreparationError("Remote refs moved or are missing; freeze new inputs")


def _write(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _clean(root: Path) -> None:
    if git(root, "status", "--porcelain", "--untracked-files=all"):
        raise PreparationError("Owned checkout must be clean")


def _reserve() -> None:
    if available_memory_bytes() < 8 * 1024**3:
        raise PreparationError("Available memory is below the required 8 GiB reserve")


def _assert_candidate(root: Path, candidate: Path) -> None:
    if (git(candidate, "rev-parse", "--show-toplevel") != str(candidate)
            or git(candidate, "rev-parse", "--path-format=absolute", "--git-common-dir")
            != git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")):
        raise PreparationError("Candidate no longer belongs to the owned repository")
    if git(candidate, "rev-parse", "--abbrev-ref", "HEAD") != "HEAD":
        raise PreparationError("Candidate is no longer detached")


def prepare(checkout: Path, manifest: Path, output: Path) -> dict:
    _git_environment()
    root = verify_skybuild(checkout)
    _clean(root)
    inputs = frozen_inputs(root, manifest.resolve())
    fingerprint = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    if output.is_symlink():
        raise PreparationError("Output must not be a symlink")
    output = output.resolve()
    # A candidate must not sit within any existing checkout, or contain one.
    worktree_lines = git(root, "worktree", "list", "--porcelain").splitlines()
    worktree_paths = [Path(line[9:]).resolve() for line in worktree_lines
                      if line.startswith("worktree ")]
    needs_new_candidate = not (output / "report.json").exists()
    if needs_new_candidate and len(worktree_paths) > MAX_WORKTREES - PREPARE_WORKTREE_RESERVE:
        raise PreparationError(
            f"Need at most {MAX_WORKTREES - PREPARE_WORKTREE_RESERVE} existing worktrees "
            f"to reserve {PREPARE_WORKTREE_RESERVE} slots for bundle preparation and integration")
    for worktree in worktree_paths:
        # A successful rerun owns exactly this detached candidate.
        if worktree == output / "candidate":
            continue
        if output.is_relative_to(worktree) or worktree.is_relative_to(output):
            raise PreparationError("Output must be isolated from existing worktrees")
    if not output.exists():
        output.mkdir(mode=0o700, parents=False)
        _write(output / "inputs.json", {"fingerprint": fingerprint, "inputs": inputs})
    owner_path = output / "inputs.json"
    if not owner_path.is_file() or owner_path.is_symlink():
        raise PreparationError("Refusing an existing unowned output directory")
    owner = json.loads(owner_path.read_text(encoding="utf-8"))
    if owner != {"fingerprint": fingerprint, "inputs": inputs}:
        raise PreparationError("Output belongs to different frozen inputs")
    lock_path = output / "preparation.lock"
    if lock_path.is_symlink():
        raise PreparationError("Refusing a symlink lock")
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise PreparationError("Preparation is already active for these inputs") from error
        candidate = output / "candidate"
        report_path = output / "report.json"
        report = {"schema": "skybuild.bundle-preparation.v1", "fingerprint": fingerprint,
                  "inputs": inputs, "candidate": str(candidate), "ok": False,
                  "gated": False, "published": False, "included": []}
        try:
            check_refs(root, inputs)
            if report_path.exists():
                previous = json.loads(report_path.read_text(encoding="utf-8"))
                if not previous.get("ok"):
                    raise PreparationError("Previous preparation failed; preserve it and use a new output")
                if (previous.get("fingerprint") != fingerprint or previous.get("inputs") != inputs
                        or previous.get("candidate") != str(candidate)):
                    raise PreparationError("Candidate report does not match frozen ownership")
                _assert_candidate(root, candidate)
                _clean(candidate)
                if (git(candidate, "rev-parse", "HEAD") != previous["candidate_head"]
                        or git(candidate, "rev-parse", "HEAD^{tree}") != previous["candidate_tree"]):
                    raise PreparationError("Candidate changed since preparation")
                for item in [inputs["target"], *inputs["members"]]:
                    git(candidate, "merge-base", "--is-ancestor", item["sha"], "HEAD")
                return previous
            if candidate.exists():
                raise PreparationError("Interrupted preparation retained; use a new output")
            _reserve()
            refs = [inputs["target"]["ref"], *(m["ref"] for m in inputs["members"])]
            git(root, "fetch", "--no-tags", "--no-write-fetch-head", "--refmap=", "origin", *refs)
            check_refs(root, inputs)
            for item in [inputs["target"], *inputs["members"]]:
                git(root, "cat-file", "-e", item["sha"] + "^{commit}")
            try:
                with reserve_worktree_slots(root, PREPARE_WORKTREE_RESERVE):
                    git(root, "-c", "core.hooksPath=/dev/null", "worktree", "add", "--detach",
                        str(candidate), inputs["target"]["sha"])
            except WorktreeCapacityError as error:
                raise PreparationError(str(error)) from error
            for member in inputs["members"]:
                _reserve()
                _assert_candidate(root, candidate)
                git(candidate, "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false",
                    "-c", "rerere.enabled=false",
                    "-c", "user.name=SkyBuild bundle preparation", "-c", "user.email=candidate@localhost",
                    "merge", "--strategy=ort", "--no-ff", "--no-edit", member["sha"])
                git(candidate, "merge-base", "--is-ancestor", member["sha"], "HEAD")
                paths = git(candidate, "diff", "--name-only", "-z", "--no-renames",
                            inputs["target"]["sha"] + "..." + member["sha"]).split("\0")[:-1]
                report["included"].append({"task_id": member["task_id"], "head": member["sha"],
                                           "changed_paths": paths})
            _clean(candidate)
            _clean(root)
            check_refs(root, inputs)
            report.update(ok=True, candidate_head=git(candidate, "rev-parse", "HEAD"),
                          candidate_tree=git(candidate, "rev-parse", "HEAD^{tree}"))
            _write(report_path, report)
            return report
        except Exception as error:
            # Never reset, abort, remove a worktree, or overwrite a completed report.
            if not report_path.exists():
                report["error"] = str(error)
                if candidate.exists():
                    try:
                        report["partial_head"] = git(candidate, "rev-parse", "HEAD")
                        report["partial_tree"] = git(candidate, "rev-parse", "HEAD^{tree}")
                        report["conflicts"] = git(candidate, "diff", "--name-only", "-z", "--diff-filter=U").split("\0")[:-1]
                    except (PreparationError, OSError, subprocess.SubprocessError):
                        report["conflicts"] = ["unavailable"]
                _write(report_path, report)
            raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = prepare(args.checkout, args.manifest, args.output)
    except (PreparationError, RepoGuardError, OSError, ValueError, KeyError, TypeError,
            subprocess.SubprocessError) as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
