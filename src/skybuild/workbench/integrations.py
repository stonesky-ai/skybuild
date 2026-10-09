"""Read-only snapshot of local Git worktrees for the development Workbench."""
from __future__ import annotations

import re
import json
import subprocess
from datetime import datetime
from pathlib import Path


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["git", "-C", str(root), *args], text=True, capture_output=True, check=False)
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result


def _task_records(root: Path) -> list[dict[str, str]]:
    records = []
    pattern = re.compile(r"^##\s+(\S+)\s+[—–-]\s+(.+?)\s*$", re.MULTILINE)
    for name in ("mastertodo.md", "deferred.md", "alreadydone.md"):
        path = root / "docs" / "design" / name
        if not path.is_file():
            continue
        records.extend({"id": match.group(1), "title": match.group(2)} for match in pattern.finditer(path.read_text(encoding="utf-8")))
    return records


def _tokens(value: str) -> set[str]:
    return {word for word in re.findall(r"[a-z0-9]+", value.lower()) if word not in {"skybuild", "task", "feature", "work"}}


def _task_for(branch: str, path: str, records: list[dict[str, str]]) -> dict[str, str]:
    task_branch = branch.removeprefix("task/") if branch.startswith("task/") else ""
    query = _tokens(task_branch or Path(path).name)
    best, score = None, 0.0
    for record in records:
        words = _tokens(record["id"] + " " + record["title"])
        overlap = len(query & words)
        candidate = overlap / max(1, len(query))
        if candidate > score:
            best, score = record, candidate
    if best and score >= 0.5:
        return {"id": best["id"], "title": best["title"], "basis": "branch name matched task ledger"}
    if task_branch:
        return {"id": "", "title": task_branch.replace("-", " "), "basis": "inferred from branch name"}
    return {"id": "", "title": "No task mapping found", "basis": "no task branch or matching ledger entry"}


def _parse_worktrees(root: Path) -> list[dict[str, str]]:
    output = _git(root, "worktree", "list", "--porcelain").stdout
    rows: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in (*output.splitlines(), ""):
        if not line:
            if current.get("path"):
                rows.append(current)
            current = {}
        elif line.startswith("worktree "):
            current["path"] = line.removeprefix("worktree ")
        elif line.startswith("HEAD "):
            current["head"] = line.removeprefix("HEAD ")
        elif line.startswith("branch refs/heads/"):
            current["branch"] = line.removeprefix("branch refs/heads/")
        elif line == "detached":
            current["detached"] = "true"
        elif line == "locked":
            current["locked"] = "true"
    return rows


def _pull_requests(root: Path) -> dict[str, dict[str, str]]:
    """Return known PR metadata keyed by head branch. GitHub failure is non-fatal."""
    remote = _git(root, "remote", "get-url", "origin", check=False).stdout.strip()
    match = re.search(r"github\.com[:/]([^/]+/[^/.]+?)(?:\.git)?$", remote)
    if not match:
        return {}
    result = subprocess.run(
        ["gh", "pr", "list", "--repo", match.group(1), "--state", "all", "--limit", "1000",
         "--json", "headRefName,baseRefName,state,number,url,mergedAt,reviewDecision"],
        text=True, capture_output=True, check=False, timeout=12,
    )
    if result.returncode:
        return {}
    try:
        prs = json.loads(result.stdout)
    except (ValueError, TypeError):
        return {}
    return {pr["headRefName"]: pr for pr in prs if pr.get("headRefName")}


def _bundles(root: Path, items: list[dict[str, str]], pull_requests: dict[str, dict[str, str]]) -> dict[str, str]:
    """Map task heads to bundle refs that contain them."""
    refs = _git(root, "for-each-ref", "--format=%(refname:short)%09%(objectname)",
                 "refs/heads/bundle", "refs/remotes/origin/bundle", check=False)
    candidates: dict[str, list[str]] = {}
    for line in refs.stdout.splitlines():
        name, _, tip = line.partition("\t")
        if not name or not tip:
            continue
        for item in items:
            branch, head = item.get("branch", ""), item.get("head", "")
            if not branch.startswith("task/") or not head:
                continue
            # Require the task head to be reachable from the bundle tip.
            if _git(root, "merge-base", "--is-ancestor", head, tip, check=False).returncode == 0:
                candidates.setdefault(branch, []).append(name)
    result = {}
    for branch, names in candidates.items():
        slug = branch.removeprefix("task/")
        def rank(name: str) -> tuple[int, int, str]:
            bundle_slug = name.removeprefix("origin/").removeprefix("bundle/")
            normalized = re.sub(r"-\d{3}$", "", bundle_slug)
            pr = pull_requests.get(name.removeprefix("origin/"), {})
            return (0 if pr.get("state") == "OPEN" else 1,
                    0 if normalized == slug else 1, name)
        result[branch] = min(names, key=rank)
    return result


def snapshot(root: Path, *, commit_limit: int = 20) -> dict:
    """Return worktrees with task-specific commits and known bundle/PR destinations."""
    root = root.resolve()
    records = _task_records(root)
    items = _parse_worktrees(root)
    pull_requests = _pull_requests(root)
    bundles = _bundles(root, items, pull_requests)
    default_base = next((ref for ref in ("refs/remotes/origin/dev-002", "refs/heads/dev-002")
                         if _git(root, "rev-parse", "--verify", ref, check=False).returncode == 0), None)
    rows = []
    for item in items:
        path = Path(item["path"])
        branch = item.get("branch", "")
        head = item.get("head", "")
        status_result = _git(path, "status", "--porcelain", check=False)
        dirty = status_result.returncode != 0 or bool(status_result.stdout.strip())
        pr = pull_requests.get(branch, {})
        bundle = bundles.get(branch, "")
        bundle_pr = pull_requests.get(bundle.removeprefix("origin/"), {}) if bundle else {}
        effective_pr = pr or bundle_pr
        target = effective_pr.get("baseRefName", "")
        comparison_base = next((ref for ref in (f"refs/remotes/origin/{target}", f"refs/heads/{target}")
                                if target and _git(root, "rev-parse", "--verify", ref, check=False).returncode == 0), None)
        if not comparison_base:
            comparison_base = default_base
        # Mainline worktrees compare to their own tip. Other worktrees compare against
        # their PR destination when known, so count and activity list share one range.
        if branch in {"main", "dev-002", "dev-003"}:
            comparison_base = head
        range_spec = f"{comparison_base}..{head}" if comparison_base and head else "HEAD"
        total = _git(path, "rev-list", "--count", range_spec, check=False)
        commit_count = int(total.stdout.strip() or "0") if total.returncode == 0 else 0
        history = _git(path, "log", f"-{commit_limit}", "--format=%H%x1f%cI%x1f%an%x1f%s", range_spec, check=False)
        commits = []
        for line in history.stdout.splitlines():
            fields = line.split("\x1f", 3)
            if len(fields) == 4:
                commits.append({"sha": fields[0][:12], "datetime": fields[1], "author": fields[2], "message": fields[3]})
        reflog = _git(path, "reflog", "show", "--format=%gs", "HEAD", check=False)
        rebase_count = sum("rebase (finish)" in line.lower() for line in reflog.stdout.splitlines()) if reflog.returncode == 0 else None
        ahead = commit_count if comparison_base else None
        merged = bool(comparison_base and head and _git(root, "merge-base", "--is-ancestor", head, comparison_base, check=False).returncode == 0)
        if effective_pr:
            task_state = f"PR #{effective_pr['number']} {effective_pr['state'].lower()} to {target}"
            if effective_pr.get("reviewDecision"):
                task_state += f" · {effective_pr['reviewDecision'].replace('_', ' ').lower()}"
        elif not comparison_base:
            task_state = "Cannot compare; destination ref unavailable"
        elif merged:
            task_state = f"Merged to {target or comparison_base.removeprefix('refs/remotes/origin/').removeprefix('refs/heads/')}"
        elif item.get("detached"):
            task_state = "Detached candidate; review and merge unknown"
        elif dirty:
            task_state = "Working changes present; review and merge unknown"
        elif ahead:
            task_state = f"Committed ({ahead} ahead of {target or comparison_base.removeprefix('refs/remotes/origin/').removeprefix('refs/heads/')}); review and merge unknown"
        else:
            task_state = "At comparison base; no unmerged commits"
        task = _task_for(branch, item["path"], records)
        latest = commits[0]["datetime"] if commits else ""
        try:
            latest_epoch = datetime.fromisoformat(latest).timestamp() if latest else 0
        except ValueError:
            latest_epoch = 0
        rows.append({
            "path": item["path"], "branch": branch or "(detached HEAD)", "head": head,
            "task": task, "commits": commits, "commit_count": commit_count,
            "comparison_base": comparison_base or "", "bundle_branch": bundle,
            "bundle_target": target if bundle else "", "bundle_pr_number": bundle_pr.get("number", ""),
            "bundle_pr_url": bundle_pr.get("url", ""), "last_activity": latest,
            "last_activity_epoch": latest_epoch, "rebase_count": rebase_count,
            "dirty": dirty, "ahead": ahead, "task_state": task_state,
        })
    rows.sort(key=lambda row: (row["task"]["title"].casefold(), row["path"].casefold()))
    return {"base": default_base or "dev-002 ref unavailable", "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"), "worktrees": rows}
