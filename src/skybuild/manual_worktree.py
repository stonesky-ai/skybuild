"""Prepare an owned manual-pilot worktree without launching or discarding work."""

import fcntl
import hashlib
import json
import re
from pathlib import Path

from .manual_assignment import AssignmentError, _git, verify_assignment


class WorktreeError(AssignmentError):
    pass


def prepare_worktree(envelope: dict, repo: Path, *, worker: str,
                     destination: Path, base_ref: str) -> dict:
    """Revalidate a pinned brief and create, or verify, its pristine owned checkout.

    The local remote-tracking development ref must already be fetched. Ownership
    records are local safety evidence, not task authority or worker admission.
    Any existing work is preserved for manual reconciliation.
    """
    repo = repo.resolve()
    snapshot = verify_assignment(envelope, repo, worker=worker)
    # Detach mutable caller input before retaining it as ownership evidence.
    envelope = json.loads(json.dumps(envelope))
    if not re.fullmatch(r"refs/remotes/origin/dev-[0-9]+", base_ref):
        raise WorktreeError("Expected an explicit origin development ref")
    destination = destination.absolute()
    if destination != destination.resolve() or "\n" in str(destination):
        raise WorktreeError("Worktree path must be canonical and must not use symlinks")
    if destination == repo or repo in destination.parents:
        raise WorktreeError("Worktree must be outside the source checkout")
    _verify_outside_checkouts(destination)
    common = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").decode().strip())
    registry = common / "skybuild-manual-worktrees"
    registry.mkdir(exist_ok=True)
    assignment = snapshot["assignment_id"]
    record_path = registry / (hashlib.sha256(assignment.encode()).hexdigest() + ".json")
    owner = {"schema": "manual-worktree-v1", "assignment": envelope,
             "destination": str(destination), "base_ref": base_ref}
    # One lock covers branch/path collisions among cooperating preparers in this
    # repository. External Git processes remain outside this manual-pilot lock.
    with (registry / "prepare.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # Revalidate inside the lock: caller dictionaries and Git refs are inputs,
        # never a trusted previously printed {verified: true} snapshot.
        snapshot = verify_assignment(envelope, repo, worker=worker)
        base = snapshot["base_sha"]
        if _git(repo, "rev-parse", "--verify", base_ref).decode().strip() != base:
            raise WorktreeError("Development base changed; reconcile the pinned assignment")
        if record_path.exists():
            try:
                recorded = json.loads(record_path.read_text())
            except (ValueError, OSError) as error:
                raise WorktreeError("Ownership record is unreadable; preserve and reconcile") from error
            if recorded != owner:
                raise WorktreeError("Assignment ownership changed; preserve and reconcile")
        else:
            if destination.exists() or destination.is_symlink():
                raise WorktreeError("Destination is not owned by this assignment")
            if _branch_exists(repo, snapshot["branch"]):
                raise WorktreeError("Task branch already exists without assignment ownership")
            # Reserve before Git writes. A crash before worktree creation can retry;
            # partial Git creation must be inspected rather than reset or removed.
            with record_path.open("x") as record:
                json.dump(owner, record, sort_keys=True)
                record.write("\n")
        _verify_outside_checkouts(destination)
        if not destination.exists():
            if _branch_exists(repo, snapshot["branch"]):
                raise WorktreeError("Owned branch has no checkout; preserve and reconcile")
            _git(repo, "worktree", "add", "-b", snapshot["branch"], str(destination), base)
        _verify_pristine(repo, destination, snapshot)
        return snapshot | {"worktree": str(destination), "head_sha": base,
                           "base_ref": base_ref, "prepared": True}


def _verify_outside_checkouts(destination: Path) -> None:
    # Inspect all parents, including ancestors of not-yet-created directories.
    # A linked worktree uses a .git file; a regular checkout uses a directory.
    for parent in destination.parents:
        marker = parent / ".git"
        # Some sandbox roots contain an empty .git sentinel, not a checkout.
        if marker.is_file() or marker.is_symlink() or (marker / "HEAD").exists():
            raise WorktreeError("Worktree must not be nested inside another Git checkout")


def _branch_exists(repo: Path, branch: str) -> bool:
    return bool(_git(repo, "for-each-ref", "--format=%(refname)", "refs/heads/" + branch).strip())


def _verify_pristine(repo: Path, destination: Path, snapshot: dict) -> None:
    try:
        root = Path(_git(destination, "rev-parse", "--show-toplevel").decode().strip()).resolve()
        expected_common = _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
        actual_common = _git(destination, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
        branch = _git(destination, "symbolic-ref", "--quiet", "HEAD").decode().strip()
        head = _git(destination, "rev-parse", "HEAD").decode().strip()
        dirty = _git(destination, "status", "--porcelain", "--untracked-files=all", "--ignored").strip()
    except AssignmentError as error:
        raise WorktreeError("Owned destination is unavailable; preserve and reconcile") from error
    if root != destination or expected_common != actual_common or branch != "refs/heads/" + snapshot["branch"]:
        raise WorktreeError("Worktree repository or branch ownership differs; preserve and reconcile")
    if dirty or head != snapshot["base_sha"]:
        raise WorktreeError("Existing work requires rescue: preserve files and commits; reconcile with dispatcher")


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assignment", type=Path, required=True)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--worker", required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--base-ref", required=True)
    args = parser.parse_args()
    try:
        envelope = json.loads(args.assignment.read_text(encoding="utf-8"))
        result = prepare_worktree(envelope, args.checkout, worker=args.worker,
                                  destination=args.destination, base_ref=args.base_ref)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (AssignmentError, OSError, ValueError, UnicodeError) as error:
        reason = str(error) if isinstance(error, AssignmentError) else "Invalid worktree input or unavailable filesystem"
        print(json.dumps({"prepared": False, "reason": reason}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
