#!/usr/bin/env python3
"""Gate an exact reviewed PR candidate, optionally merge, then verify its tree.

Review evidence is an operator-supplied artifact, not a trusted publisher
qualification. GitHub's merge API has no atomic expected-base CAS: another writer
can move the base after the last check. A post-publication mismatch is reported
as failure and cannot undo publication. Serialize publishers externally.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from _repo_guard import RepoGuardError, verify_skybuild, verify_skybuild_remote


def run(argv, cwd):
    result = subprocess.run(argv, cwd=cwd, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}): {argv[0]} {argv[1] if len(argv) > 1 else ''}: {result.stderr[-1500:]}")
    return result.stdout.strip()


def run_gate(argv, cwd):
    """Retain compact gate evidence without exposing captured test output."""
    process = subprocess.run(argv, cwd=cwd, text=True, capture_output=True)
    try:
        evidence = json.loads(process.stdout)
    except ValueError:
        evidence = {"exit_code": process.returncode}
    if process.returncode or isinstance(evidence, dict) and evidence.get("ok") is False:
        log = evidence.get("log") if isinstance(evidence, dict) else None
        detail = f"Gate failed (exit {process.returncode})"
        if isinstance(log, str) and not any(ord(char) < 32 for char in log):
            detail += "; log=" + log
        raise RuntimeError(detail)
    return evidence


def integrate(args):
    root = verify_skybuild(args.checkout)
    if args.merge and args.gate_argv:
        raise RuntimeError("Custom gates are validation-only; --merge requires the project disposable PostgreSQL gate")
    verify_skybuild_remote(root, args.remote)
    if args.repo not in (None, "stonesky-ai/skybuild"):
        raise RuntimeError("Expected an explicit SkyBuild checkout with stonesky-ai/skybuild origin")
    evidence = args.review_evidence.read_bytes()
    if not evidence.strip():
        raise RuntimeError("Review evidence must be nonempty and identify the reviewed head and reviewer")
    if args.expected_head.encode() not in evidence:
        raise RuntimeError("Review evidence must contain the exact expected head")
    if args.expected_base.encode() not in evidence:
        raise RuntimeError("Review evidence must contain the exact expected base")
    gh_repo = ["--repo", "stonesky-ai/skybuild"]

    def view():
        info = json.loads(run(["gh", "pr", "view", str(args.pr), *gh_repo, "--json",
                               "state,isDraft,headRefOid,baseRefName,mergeable,mergeStateStatus"], root))
        if (info["state"] != "OPEN" or info["isDraft"] or info["headRefOid"] != args.expected_head
                or info["baseRefName"] != args.base or info["mergeable"] != "MERGEABLE"
                or info["mergeStateStatus"] != "CLEAN"):
            raise RuntimeError("PR is not an open CLEAN/MERGEABLE reviewed head on the expected base")
        return info

    view()
    run(["git", "check-ref-format", "refs/heads/" + args.base], root)
    # Fetch by ref, then independently prove the advertised PR head.
    run(["git", "fetch", "--no-tags", args.remote, "refs/heads/" + args.base,
         f"refs/pull/{args.pr}/head"], root)
    def remote_oid(ref):
        lines = run(["git", "ls-remote", "--exit-code", args.remote, ref], root).splitlines()
        if len(lines) != 1:
            raise RuntimeError("Expected exactly one remote ref")
        return lines[0].split()[0]
    base = remote_oid("refs/heads/" + args.base)
    head = remote_oid(f"refs/pull/{args.pr}/head")
    if base != args.expected_base:
        raise RuntimeError("Remote base differs from the expected reviewed base")
    if head != args.expected_head:
        raise RuntimeError("Remote PR head differs from reviewed head")
    # Resolve both objects locally; fail if fetch and remote inspection raced.
    for oid in (base, head):
        run(["git", "cat-file", "-e", oid + "^{commit}"], root)
    with tempfile.TemporaryDirectory(prefix="skybuild-pr-candidate-") as directory:
        candidate = Path(directory) / "checkout"
        added = False
        try:
            run(["git", "worktree", "add", "--detach", str(candidate), base], root)
            added = True
            run(["git", "-c", "user.name=SkyBuild candidate", "-c", "user.email=candidate@localhost",
                 "merge", "--no-ff", "--no-edit", head], candidate)
            tree = run(["git", "rev-parse", "HEAD^{tree}"], candidate)
            gate = args.gate_argv or [sys.executable, str(root / "scripts/disposable_pg_gate.py"),
                                      "--checkout", str(candidate), "--min-available-gib", "10"]
            gate = [word.replace("{checkout}", str(candidate)) for word in gate]
            gate_result = run_gate(gate, candidate)
            if run(["git", "status", "--porcelain"], candidate):
                raise RuntimeError("Gate modified the candidate checkout")
            if run(["git", "rev-parse", "HEAD^{tree}"], candidate) != tree:
                raise RuntimeError("Gate changed the candidate tree")
            view()
            if remote_oid("refs/heads/" + args.base) != base or remote_oid(f"refs/pull/{args.pr}/head") != head:
                raise RuntimeError("Remote refs changed during validation")
            result = {"ok": True, "merged": False, "pr": args.pr, "head": head, "base": base,
                      "expected_base": args.expected_base, "gate": gate_result,
                      "candidate_tree": tree, "review_sha256": hashlib.sha256(evidence).hexdigest(),
                      "atomic_expected_base": False}
            if args.merge:
                run(["gh", "pr", "merge", str(args.pr), *gh_repo, "--merge", "--match-head-commit", head], root)
                published = json.loads(run(["gh", "pr", "view", str(args.pr), *gh_repo,
                                            "--json", "state,mergeCommit"], root))
                if published["state"] != "MERGED" or not published["mergeCommit"]:
                    raise RuntimeError("Merge publication is not confirmed")
                commit = published["mergeCommit"]["oid"]
                run(["git", "fetch", "--no-tags", args.remote, "refs/heads/" + args.base], root)
                if run(["git", "rev-parse", commit + "^{tree}"], root) != tree:
                    raise RuntimeError("Published merge tree differs from the tested candidate; publication already occurred")
                run(["git", "merge-base", "--is-ancestor", commit, "FETCH_HEAD"], root)
                result.update(merged=True, published_commit=commit)
            return result
        finally:
            if added:
                run(["git", "worktree", "remove", "--force", str(candidate)], root)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--expected-base", required=True)
    parser.add_argument("--review-evidence", type=Path, required=True)
    parser.add_argument("--repo")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--merge", action="store_true", help="Publish the validated merge (requires existing authority)")
    parser.add_argument("--gate-argv", nargs=argparse.REMAINDER,
                        help="Validation-only gate argv (cannot use --merge); use {checkout}; put this option last")
    args = parser.parse_args(argv)
    if args.pr <= 0 or any(not re.fullmatch(r"[0-9a-f]{40}", oid)
                           for oid in (args.expected_head, args.expected_base)):
        parser.error("Provide a positive PR number and full lowercase 40-character expected head and base")
    print(json.dumps(integrate(args), separators=(",", ":")))


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, RepoGuardError, ValueError, KeyError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, separators=(",", ":")))
        raise SystemExit(1)
