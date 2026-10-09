#!/usr/bin/env python3
"""Remove merged development branches after a reviewed mainline closeout.

Default mode prints a plan. Applying it requires an exact expected main SHA,
no open PRs, no extra worktrees, and merge/tree evidence for every branch.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

from _repo_guard import RepoGuardError, verify_skybuild, verify_skybuild_remote


class CloseoutError(Exception):
    pass


def command(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, cwd=repo, text=True, capture_output=True, check=False)
    if check and result.returncode:
        raise CloseoutError(f"{' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return command(repo, "git", *args, check=check)


def remote_heads(repo: Path, remote: str) -> dict[str, str]:
    heads = {}
    for line in git(repo, "ls-remote", "--heads", remote).stdout.splitlines():
        oid, ref = line.split("\t", 1)
        heads[ref.removeprefix("refs/heads/")] = oid
    return heads


def local_heads(repo: Path) -> list[str]:
    return git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads").stdout.splitlines()


def local_oids(repo: Path) -> dict[str, str]:
    return dict(line.split(" ", 1) for line in git(
        repo, "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads").stdout.splitlines())


def open_prs(repo: Path) -> list[dict]:
    output = command(repo, "gh", "pr", "list", "--state", "open", "--limit", "1000",
                     "--json", "number,headRefName,baseRefName").stdout
    return json.loads(output)


def require_clean_checkout(repo: Path, target: str) -> None:
    root = Path(git(repo, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    if root != repo:
        raise CloseoutError(f"checkout path is {root}, expected {repo}")
    if git(repo, "status", "--porcelain").stdout.strip():
        raise CloseoutError("checkout has uncommitted or untracked files")
    branch = git(repo, "branch", "--show-current").stdout.strip()
    if branch != target:
        raise CloseoutError(f"checkout is on {branch or 'detached HEAD'}, expected {target}")
    worktrees = [line for line in git(repo, "worktree", "list", "--porcelain").stdout.splitlines()
                 if line.startswith("worktree ")]
    if worktrees != [f"worktree {repo}"]:
        raise CloseoutError("other worktrees remain; remove only owned clean worktrees first")


def plan(repo: Path, remote: str, target: str, expected_target: str | None) -> dict:
    require_clean_checkout(repo, target)
    verify_skybuild_remote(repo, remote)
    git(repo, "fetch", remote, "--prune")
    heads = remote_heads(repo, remote)
    if target not in heads:
        raise CloseoutError(f"remote target {target} is absent")
    if expected_target and heads[target] != expected_target:
        raise CloseoutError(f"remote {target} changed: {heads[target]}")
    prs = open_prs(repo)
    if prs:
        raise CloseoutError(f"open PRs remain: {json.dumps(prs, sort_keys=True)}")
    remote_delete = sorted(name for name in heads if name != target)
    locals_at_plan = local_oids(repo)
    local_delete = sorted(name for name in locals_at_plan if name != target)
    target_oid = heads[target]
    for name, oid in heads.items():
        if git(repo, "cat-file", "-e", f"{oid}^{{commit}}", check=False).returncode:
            raise CloseoutError(f"advertised remote commit is not fetched: {name} {oid}")
    for name in remote_delete:
        if git(repo, "merge-base", "--is-ancestor", heads[name], target_oid, check=False).returncode:
            raise CloseoutError(f"remote branch is not merged into {target}: {name}")
    for name in local_delete:
        if git(repo, "merge-base", "--is-ancestor", locals_at_plan[name], target_oid, check=False).returncode:
            raise CloseoutError(f"local branch is not merged into {target}: {name}")
    return {"target": target, "target_oid": heads[target], "remote_delete": remote_delete,
            "local_delete": local_delete, "remote_oids": heads, "local_oids": locals_at_plan}


def apply(repo: Path, remote: str, target: str, expected_target: str) -> dict:
    result = plan(repo, remote, target, expected_target)
    require_clean_checkout(repo, target)
    verify_skybuild_remote(repo, remote)
    current = remote_heads(repo, remote)
    if current != result["remote_oids"] or local_oids(repo) != result["local_oids"] or open_prs(repo):
        raise CloseoutError("remote refs, local branches or open PRs changed after preflight")
    if result["remote_delete"]:
        args = ["git", "push", "--atomic"]
        for name in result["remote_delete"]:
            args.append(f"--force-with-lease=refs/heads/{name}:{current[name]}")
        args.append(remote)
        for name in result["remote_delete"]:
            args.append(f":refs/heads/{name}")
        command(repo, *args)
    for name in result["local_delete"]:
        git(repo, "update-ref", "-d", f"refs/heads/{name}", result["local_oids"][name])
    git(repo, "fetch", remote, "--prune")
    final_remote = remote_heads(repo, remote)
    final_local = local_heads(repo)
    if final_remote != {target: expected_target} or final_local != [target] or open_prs(repo):
        raise CloseoutError("closeout needs reconciliation; inspect remaining refs and PRs")
    return {"result": "closed", "target": target, "target_oid": expected_target,
            "deleted_remote": result["remote_delete"], "deleted_local": result["local_delete"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, default=Path.cwd())
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--target", default="main")
    parser.add_argument("--expected-target", help="exact remote target SHA, required with --apply")
    parser.add_argument("--apply", action="store_true", help="delete verified merged branches")
    args = parser.parse_args()
    repo = args.checkout.resolve()
    try:
        repo = verify_skybuild(repo)
        if args.apply:
            if not args.expected_target:
                raise CloseoutError("--apply requires --expected-target")
            result = apply(repo, args.remote, args.target, args.expected_target)
        else:
            result = {"result": "plan", **plan(repo, args.remote, args.target, args.expected_target)}
            result.pop("remote_oids")
            result.pop("local_oids")
        print(json.dumps(result, sort_keys=True))
        return 0
    except (CloseoutError, RepoGuardError, OSError, ValueError) as error:
        print(json.dumps({"result": "blocked", "reason": str(error)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
