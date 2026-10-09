"""Read-only validation of a pinned manual-worker assignment."""

import hashlib
import json
import re
import subprocess
from pathlib import Path, PurePosixPath

from .contracts import valid_identifier


class AssignmentError(ValueError):
    pass


_SHA = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_ASSIGNMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_BRANCH = re.compile(r"(?:feature|task)/[A-Za-z0-9][A-Za-z0-9._/-]{0,127}\Z")
_ORIGIN = re.compile(r"(?:https://(?:[^/@]+@)?github\.com/|ssh://git@github\.com/|git@github\.com:)"
                     r"stonesky-ai/skybuild(?:\.git)?/?\Z", re.IGNORECASE)


def _path(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise AssignmentError("Assignment has an invalid repository path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {".", "..", ".git"} for part in value.split("/")):
        raise AssignmentError("Assignment path must stay inside the repository")
    if str(path) != value or value.endswith("/"):
        raise AssignmentError("Assignment path must be canonical")
    return value


def _git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, check=False)
    if result.returncode:
        raise AssignmentError("Pinned assignment Git object is unavailable")
    return result.stdout


def verify_assignment(envelope: dict, repo: Path, *, worker: str, git_runner=None) -> dict:
    """Check a Cord snapshot against committed Git bytes; never grant task authority."""
    git = git_runner or _git
    required = {"schema", "assignment_id", "task_id", "worker", "dispatcher", "base_sha",
                "brief_path", "brief_sha256", "branch", "owned_paths", "checks", "model_limit"}
    if not isinstance(envelope, dict) or set(envelope) != required:
        raise AssignmentError("Assignment fields do not match manual-work-v1")
    if envelope["schema"] != "manual-work-v1":
        raise AssignmentError("Unknown assignment schema")
    if not isinstance(worker, str) or not worker or envelope["worker"] != worker:
        raise AssignmentError("Assignment names another worker")
    for field in ("assignment_id", "dispatcher"):
        if not isinstance(envelope[field], str) or not _ASSIGNMENT.fullmatch(envelope[field]):
            raise AssignmentError(f"Invalid {field}")
    task_id = envelope["task_id"]
    if not isinstance(task_id, str) or not task_id.startswith("SKYBUILD-") or not valid_identifier(task_id):
        raise AssignmentError("Invalid ledger task ID")
    base = envelope["base_sha"]
    digest = envelope["brief_sha256"]
    if not isinstance(base, str) or not _SHA.fullmatch(base) or not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise AssignmentError("Assignment needs exact Git and brief hashes")
    branch = envelope["branch"]
    if not isinstance(branch, str) or not _BRANCH.fullmatch(branch) or "//" in branch or ".." in branch:
        raise AssignmentError("Invalid task branch")
    git(repo, "check-ref-format", "--branch", branch)
    paths = envelope["owned_paths"]
    if (not isinstance(paths, list) or not paths or not all(isinstance(value, str) for value in paths)
            or len(paths) != len(set(paths))):
        raise AssignmentError("Owned paths must be a nonempty distinct list")
    paths = [_path(value) for value in paths]
    if not isinstance(envelope["checks"], list) or not envelope["checks"] or not all(
            isinstance(item, str) and 0 < len(item) <= 300 for item in envelope["checks"]):
        raise AssignmentError("Assignment needs bounded acceptance checks")
    if not isinstance(envelope["model_limit"], str) or not 0 < len(envelope["model_limit"]) <= 200:
        raise AssignmentError("Assignment needs a model limit")
    brief_path = _path(envelope["brief_path"])
    if not brief_path.startswith("docs/design/assignments/") or not brief_path.endswith(".json"):
        raise AssignmentError("Brief must be a committed assignment JSON file")
    repo = repo.resolve()
    root = Path(git(repo, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    if root != repo or not (repo / "docs/design/architecture.md").is_file():
        raise AssignmentError("Expected the exact SkyBuild checkout")
    remotes = (git(repo, "remote", "get-url", "--all", "origin").decode().splitlines()
               + git(repo, "remote", "get-url", "--push", "--all", "origin").decode().splitlines())
    if len(remotes) < 2 or any(not _ORIGIN.fullmatch(url) for url in remotes):
        raise AssignmentError("Checkout origin is not SkyBuild")
    resolved = git(repo, "rev-parse", "--verify", f"{base}^{{commit}}").decode().strip()
    if resolved != base:
        raise AssignmentError("Assignment base is not the pinned commit")
    mode = git(repo, "ls-tree", base, "--", brief_path).decode().split()
    if not mode or mode[0] != "100644":
        raise AssignmentError("Brief must be a committed regular file")
    brief = git(repo, "show", f"{base}:{brief_path}")
    try:
        document = json.loads(brief.decode("utf-8"))
    except (UnicodeError, ValueError) as error:
        raise AssignmentError("Brief must contain UTF-8 JSON") from error
    if hashlib.sha256(brief).hexdigest() != digest:
        raise AssignmentError("Brief hash differs from assignment")
    committed_fields = {"assignment_id", "task_id", "worker", "dispatcher", "branch",
                        "owned_paths", "checks", "model_limit"}
    if (not isinstance(document, dict) or set(document) != committed_fields | {"schema", "next_action"}
            or document.get("schema") != "manual-work-brief-v1"
            or not isinstance(document.get("next_action"), str) or not document["next_action"].strip()
            or any(document[field] != envelope[field] for field in committed_fields)):
        raise AssignmentError("Cord assignment differs from committed brief")
    return {"assignment_id": envelope["assignment_id"], "task_id": task_id, "worker": worker,
            "base_sha": base, "brief_path": brief_path, "branch": branch, "owned_paths": paths,
            "verified": True, "authority": "markdown"}


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assignment", type=Path, required=True)
    parser.add_argument("--checkout", type=Path, default=Path.cwd())
    parser.add_argument("--worker", required=True)
    args = parser.parse_args()
    try:
        envelope = json.loads(args.assignment.read_text(encoding="utf-8"))
        print(json.dumps(verify_assignment(envelope, args.checkout, worker=args.worker), sort_keys=True))
        return 0
    except (AssignmentError, OSError, ValueError, UnicodeError) as error:
        print(json.dumps({"verified": False, "reason": str(error) if isinstance(error, AssignmentError) else "Invalid assignment input"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
