"""Verify and remove merged or never-started local worktrees."""
from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

from . import integrations


_LOG_DIR = ".workbench-state"
_LOG_FILE = "worktree-cleaner.jsonl"
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


def _targets(root: Path) -> list[tuple[str, str]]:
    refs = integrations._git(
        root, "for-each-ref", "--format=%(refname:short)%09%(objectname)",
        "refs/heads/main", "refs/heads/dev-*", "refs/remotes/origin/main",
        "refs/remotes/origin/dev-*", check=False,
    )
    return [tuple(line.split("\t", 1)) for line in refs.stdout.splitlines() if "\t" in line]


def _process_cwds() -> tuple[list[tuple[int, str]], int]:
    found: list[tuple[int, str]] = []
    unreadable = 0
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return found, 1
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            cwd = os.readlink(entry / "cwd")
        except FileNotFoundError:
            continue
        except PermissionError:
            unreadable += 1
            continue
        except OSError:
            continue
        found.append((int(entry.name), os.path.normpath(cwd)))
    return found, unreadable


def _in_use(path: str, processes: list[tuple[int, str]]) -> list[int]:
    prefix = path.rstrip(os.sep) + os.sep
    return [pid for pid, cwd in processes if cwd == path or cwd.startswith(prefix)]


def _identity(path: str, branch: str, head: str) -> str:
    value = f"{path}\0{branch}\0{head}".encode()
    return hashlib.sha256(value).hexdigest()


def _never_started(root: Path, branch: str, head: str, targets: list[tuple[str, str]]) -> bool:
    """True only when branch reflog proves HEAD never moved after creation."""
    if not branch or branch.startswith("("):
        return False
    history = integrations._git(
        root, "reflog", "show", "--reverse", "--format=%H%x1f%gs", f"refs/heads/{branch}", check=False,
    )
    if not history.stdout:
        return False
    first = history.stdout.splitlines()[0].split("\x1f", 1)
    if len(first) != 2 or first[0] != head or "created from" not in first[1].lower():
        return False
    source_is_main_dev = any(name in first[1] or name.removeprefix("origin/") in first[1] for name, _ in targets)
    if not source_is_main_dev:
        return False
    # Require creation point to remain reachable from a current main/dev ref.
    return any(integrations._git(root, "merge-base", "--is-ancestor", head, name, check=False).returncode == 0
               for name, _ in targets)


def _inventory(root: Path) -> tuple[list[dict], int]:
    root = root.resolve()
    worktrees = integrations._parse_worktrees(root)
    targets = _targets(root)
    processes, unreadable = _process_cwds()
    rows = []
    protected = {"main"}
    for name, _ in targets:
        short = name.removeprefix("origin/")
        if short.startswith("dev-"):
            protected.add(short)
    for item in worktrees:
        path = os.path.normpath(str(Path(item["path"]).resolve()))
        branch, head = item.get("branch", ""), item.get("head", "")
        reasons = []
        target = ""
        if Path(path) == root:
            reasons.append("Active Workbench checkout is protected")
        if branch in protected or branch.startswith(("dev-", "bundle/")):
            reasons.append("Shared integration branch is protected")
        is_locked = bool(item.get("locked"))
        if is_locked:
            reasons.append("Worktree is locked")
        status = integrations._git(Path(path), "status", "--porcelain", check=False)
        status_clean = status.returncode == 0 and not status.stdout.strip()
        if status.returncode != 0:
            reasons.append("Could not read worktree status")
        elif status.stdout.strip():
            reasons.append("Uncommitted or untracked files are present")
        pids = _in_use(path, processes)
        if pids:
            reasons.append("Process working directory is inside this worktree: " + ", ".join(map(str, pids)))
        if unreadable:
            reasons.append(f"Cannot inspect {unreadable} process working director{ 'y' if unreadable == 1 else 'ies' }")
        if not branch:
            reasons.append("Detached worktree has no task branch evidence")
        if not head or not targets:
            reasons.append("Cannot verify against a local main/dev ref")
        contained = next((name for name, _tip in targets
                          if integrations._git(root, "merge-base", "--is-ancestor", head, name, check=False).returncode == 0), "") if head else ""
        compare_ref = contained or next((name for name, _tip in reversed(targets)
                                         if name.endswith("/dev-003") or name == "dev-003"), "")
        if not compare_ref and targets:
            compare_ref = targets[0][0]
        missing_commits = None
        if head and compare_ref:
            count = integrations._git(root, "rev-list", "--count", f"{compare_ref}..{head}", check=False)
            if count.returncode == 0:
                try:
                    missing_commits = int(count.stdout.strip())
                except ValueError:
                    pass
        process_safe = not pids and not unreadable
        proof = [
            {"label": "HEAD ancestry", "passed": bool(contained),
             "detail": f"contained in {contained}" if contained else f"not contained in {compare_ref or 'any main/dev ref'}"},
            {"label": "Commits missing from comparison", "passed": missing_commits == 0,
             "detail": f"{missing_commits} commit(s) absent from {compare_ref}" if missing_commits is not None
             else "could not count commits against a main/dev ref"},
            {"label": "Worktree safety", "passed": status_clean and process_safe and not is_locked,
             "detail": ("clean, unlocked, and no process is using it" if status_clean and process_safe and not is_locked
                        else "; ".join(((["dirty or status unavailable"] if not status_clean else [])
                                        + (["process is using it"] if pids else [])
                                        + (["process data unreadable"] if unreadable else [])
                                        + (["locked"] if is_locked else []))))},
        ]
        if not reasons:
            if _never_started(root, branch, head, targets):
                reason = "No commits since branch creation; no process is using this worktree"
            else:
                for name, _tip in targets:
                    if integrations._git(root, "merge-base", "--is-ancestor", head, name, check=False).returncode == 0:
                        target = name
                        break
                if target:
                    reason = f"HEAD is already contained in {target}"
                else:
                    reason = "No completed merge found, and branch has commits"
                    reasons.append(reason)
        else:
            reason = "; ".join(reasons)
        eligible = not reasons
        if eligible and target:
            reason = f"Merged to {target}"
        rows.append({
            "id": _identity(path, branch, head), "path": path, "branch": branch or "(detached HEAD)",
            "head": head[:12], "head_sha": head, "reason": reason,
            "eligible": not reasons,
            "target": target,
            "proof": proof,
        })
    return rows, len(worktrees)


def preview(root: Path) -> dict:
    rows, total = _inventory(root)
    candidates = [row for row in rows if row["eligible"]]
    return {
        "worktree_count": total,
        "candidate_count": len(candidates),
        "blocked_count": total - len(candidates),
        "candidates": candidates,
        "blocked": [row for row in rows if not row["eligible"]],
        "cleanup_log": cleanup_log_tail(root),
    }


def _open_log(root: Path, flags: int) -> int:
    root_fd = os.open(Path(root).resolve(), os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW | _CLOEXEC)
    try:
        try:
            os.mkdir(_LOG_DIR, mode=0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
        directory_fd = os.open(_LOG_DIR, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW | _CLOEXEC,
                               dir_fd=root_fd)
    finally:
        os.close(root_fd)
    try:
        directory = os.fstat(directory_fd)
        if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid():
            raise PermissionError("Worktree Cleaner state directory is not privately owned")
        os.fchmod(directory_fd, 0o700)
        descriptor = os.open(_LOG_FILE, flags | _NOFOLLOW | _CLOEXEC | os.O_NONBLOCK,
                             0o600, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise PermissionError("Worktree Cleaner log is not a private regular file")
        os.fchmod(descriptor, 0o600)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _append_cleanup_log(root: Path, result: dict) -> None:
    descriptor = _open_log(root, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    try:
        remaining = memoryview((json.dumps(result, sort_keys=True) + "\n").encode("utf-8"))
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("short write to Worktree Cleaner log")
            remaining = remaining[written:]
    finally:
        os.close(descriptor)


def cleanup_log_tail(root: Path, limit: int = 30) -> list[dict]:
    try:
        descriptor = _open_log(root, os.O_RDONLY)
    except FileNotFoundError:
        return []
    try:
        info = os.fstat(descriptor)
        os.lseek(descriptor, max(0, info.st_size - 64 * 1024), os.SEEK_SET)
        chunks = []
        remaining = 64 * 1024
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(descriptor)
    result = []
    for line in b"".join(chunks).decode("utf-8", "replace").splitlines()[-limit:]:
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                result.append(row)
        except ValueError:
            continue
    return list(reversed(result))


def clean(root: Path, candidate_id: str) -> dict:
    """Recheck eligibility immediately before a non-forced Git worktree removal."""
    rows, _total = _inventory(root)
    candidate = next((row for row in rows if row["id"] == candidate_id and row["eligible"]), None)
    if candidate is None:
        raise ValueError("Worktree no longer meets cleanup conditions. Refresh the cleaner preview.")
    path = candidate["path"]
    branch = candidate["branch"]
    expected_head = candidate["head_sha"]
    # Git refuses removal if the worktree gained changes after the verification scan.
    result = integrations._git(root, "worktree", "remove", "--", path, check=False)
    if result.returncode:
        raise ValueError("Git refused to remove the worktree; it may have changed. Refresh the cleaner preview.")
    # Delete only the exact branch tip verified above, and only after successful removal.
    branch_ref = f"refs/heads/{branch}"
    deleted_branch = False
    if branch and not branch.startswith("("):
        ref = integrations._git(root, "rev-parse", "--verify", branch_ref, check=False)
        if ref.returncode == 0:
            expected = integrations._git(root, "update-ref", "-d", branch_ref, expected_head, check=False)
            deleted_branch = expected.returncode == 0
    result = {
        "removed": True, "path": path, "branch": branch,
        "branch_deleted": deleted_branch,
        "message": f"Removed worktree {path}" + (f" and local branch {branch}." if deleted_branch else "."),
        "head": expected_head,
        "target": candidate.get("target", ""),
        "proof": candidate["proof"],
        "cleaned_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    try:
        _append_cleanup_log(root, result)
        result["log_written"] = True
    except OSError as error:
        result["log_written"] = False
        result["log_error"] = f"Worktree was removed, but the cleanup log could not be written ({type(error).__name__})."
    return result


def clean_selected(root: Path, candidate_ids: list[str]) -> dict:
    if not candidate_ids or len(candidate_ids) > 100 or len(set(candidate_ids)) != len(candidate_ids):
        raise ValueError("Select between 1 and 100 distinct worktrees.")
    results = []
    for candidate_id in candidate_ids:
        try:
            results.append(clean(root, candidate_id))
        except (ValueError, OSError, RuntimeError) as error:
            results.append({"removed": False, "candidate_id": candidate_id, "message": str(error)})
    return {"results": results, "removed_count": sum(bool(row.get("removed")) for row in results),
            "cleanup_log": cleanup_log_tail(root)}
