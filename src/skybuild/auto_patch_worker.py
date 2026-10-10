"""One bounded CPU worker for an administrator-approved, hash-pinned patch.

The worker has only its project-scoped worker credential. It cannot approve its
own review, gate, publication or task acceptance. A failed or uncertain write
leaves its private attempt directory for explicit reconciliation.
"""

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

from .client import Client, ClientError, ca_file_sha256
from .fleet_preflight import _resolved_addresses, _token_from_file, probe_private_api
from .manual_assignment import _path, verify_assignment
from .manual_cord import receive_assignment, renew_assignment, send_result, _workflow_path
from .manual_dispatch import _private_endpoint, _state_directory


class PatchWorkerError(ValueError):
    pass


def _git(repo: Path | None, *args: str, timeout: int = 30,
         askpass: Path | None = None, git_token_file: Path | None = None) -> bytes:
    if any(name.startswith("GIT_") and name != "GIT_PAGER" for name in os.environ):
        raise PatchWorkerError("Inherited Git environment requires reconciliation")
    environment = {name: os.environ[name] for name in ("HOME", "PATH", "LANG", "LC_ALL") if name in os.environ}
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null",
                       GIT_TERMINAL_PROMPT="0", GIT_LFS_SKIP_SMUDGE="1")
    if askpass is not None and git_token_file is not None:
        environment["GIT_ASKPASS"] = str(askpass)
        environment["SKYBUILD_GIT_TOKEN_FILE"] = str(git_token_file)
    command = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.attributesFile=/dev/null",
               "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false", *args]
    try:
        result = subprocess.run(command, cwd=repo, env=environment, capture_output=True,
                                timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise PatchWorkerError("Git operation outcome is unknown; preserve attempt") from None
    if result.returncode:
        raise PatchWorkerError("Git operation failed; preserve attempt")
    return result.stdout


def _approved_task(task: dict, assignment: dict, patch_sha256: str) -> None:
    if (not isinstance(task, dict) or task.get("task_id") != assignment["task_id"]
            or task.get("status") != "ready" or type(task.get("revision")) is not int
            or task.get("revision") != assignment["task_revision"]):
        raise PatchWorkerError("Task is not the pinned Ready task")
    metadata = task.get("metadata")
    approval = metadata.get("_skybuild_cpu_patch") if isinstance(metadata, dict) else None
    if approval != {"schema": "skybuild.cpu-patch.v1", "sha256": patch_sha256,
                    "assignment_id": assignment["assignment_id"]}:
        raise PatchWorkerError("Task has no matching administrative patch approval")
    acceptance = task.get("acceptance_criteria")
    if (not isinstance(acceptance, list) or not any(
            isinstance(item, str) and "deterministic approved patch" in item.lower()
            for item in acceptance)):
        raise PatchWorkerError("Task acceptance does not permit a deterministic approved patch")
    workflow = metadata.get("_skybuild_workflow")
    petri = workflow.get("petri") if isinstance(workflow, dict) else None
    token = petri.get("token") if isinstance(petri, dict) else None
    if (not isinstance(token, dict) or token.get("place") != "ready"
            or token.get("pending_action") is not None or token.get("superseded")):
        raise PatchWorkerError("Petri task is not Ready")


def _patch_bytes(path: Path, expected_sha256: str) -> bytes:
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise PatchWorkerError("Patch digest is invalid")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 65536
                or info.st_mode & 0o022):
            raise PatchWorkerError("Patch file is unsafe or exceeds 64 KiB")
        data = os.read(descriptor, 65537)
    finally:
        os.close(descriptor)
    if len(data) > 65536 or hashlib.sha256(data).hexdigest() != expected_sha256:
        raise PatchWorkerError("Approved patch bytes changed")
    return data


def _record(directory: Path, name: str, payload: dict) -> None:
    path = directory / name
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _askpass(directory: Path, git_token_file: Path) -> Path:
    """Use an operator-owned credential file without exposing token bytes in argv."""
    if not git_token_file.is_absolute() or "\n" in str(git_token_file):
        raise PatchWorkerError("Git token path must be absolute")
    descriptor = os.open(git_token_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or not 0 < info.st_size <= 1024):
            raise PatchWorkerError("Git token file must be private and owned")
    finally:
        os.close(descriptor)
    path = directory / "git-askpass.sh"
    script = ("#!/bin/sh\n"
              "case \"$1\" in\n"
              "  *Username*) printf '%s\\n' 'x-access-token' ;;\n"
              "  *Password*) exec /bin/cat -- \"$SKYBUILD_GIT_TOKEN_FILE\" ;;\n"
              "  *) exit 1 ;;\n"
              "esac\n")
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o700)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(script)
        stream.flush()
        os.fsync(stream.fileno())
    return path


def _changed_paths(repo: Path, base: str) -> list[str]:
    paths = _git(repo, "diff", "--no-renames", "--name-only", "-z", base, "HEAD")
    return sorted(os.fsdecode(value) for value in paths.split(b"\0") if value)


def _patch_paths(repo: Path, base: str, patch_file: Path, owned_paths: list[str]) -> list[str]:
    """Check every patch destination before allowing a filesystem write."""
    rows = _git(repo, "apply", "--numstat", "-z", str(patch_file)).split(b"\0")
    if rows[-1] != b"":
        raise PatchWorkerError("Patch path report is incomplete")
    paths = []
    for row in rows[:-1]:
        fields = row.split(b"\t", 2)
        if len(fields) != 3:
            raise PatchWorkerError("Patch path report is malformed")
        if not fields[0].isdigit() or not fields[1].isdigit():
            raise PatchWorkerError("Binary or ambiguous patch is unsupported")
        try:
            path = fields[2].decode("utf-8")
            _path(path)
        except (UnicodeError, ValueError):
            raise PatchWorkerError("Patch path is unsafe") from None
        if (path not in owned_paths or path.split("/")[-1] in
                {".gitattributes", ".gitmodules", ".gitignore"}
                or "\n" in path or "\r" in path):
            raise PatchWorkerError("Patch path is outside exact assigned files")
        entry = _git(repo, "ls-tree", "-z", base, "--", path)
        if not entry.startswith(b"100644 blob ") or not entry.endswith(b"\t" + path.encode() + b"\0"):
            raise PatchWorkerError("Patch target must be a tracked regular source file")
        paths.append(path)
    if not paths or len(paths) != len(set(paths)):
        raise PatchWorkerError("Patch must change distinct tracked files")
    return sorted(paths)


def _clone_and_apply(repo: Path, destination: Path, assignment: dict, patch: bytes) -> tuple[str, list[str]]:
    if destination.exists() or destination.is_symlink() or not destination.parent.is_dir():
        raise PatchWorkerError("Worker checkout exists; reconcile before another attempt")
    origin = _git(repo, "remote", "get-url", "origin").decode().strip()
    push_origin = _git(repo, "remote", "get-url", "--push", "origin").decode().strip()
    if origin != "https://github.com/stonesky-ai/skybuild.git" or push_origin != origin:
        raise PatchWorkerError("Worker source origin differs")
    _git(None, "clone", "--no-checkout", origin, str(destination), timeout=120)
    if (_git(destination, "remote", "get-url", "origin").decode().strip() != origin
            or _git(destination, "remote", "get-url", "--push", "origin").decode().strip() != origin):
        raise PatchWorkerError("Worker clone origin differs")
    base = assignment["base_sha"]
    _git(destination, "cat-file", "-e", base + "^{commit}")
    _git(destination, "switch", "-c", assignment["branch"], base)
    if _git(destination, "rev-parse", "HEAD").decode().strip() != base:
        raise PatchWorkerError("Worker checkout did not select pinned base")
    patch_file = destination.parent / "approved.patch"
    patch_file.write_bytes(patch)
    patch_file.chmod(0o600)
    try:
        planned_paths = _patch_paths(destination, base, patch_file, assignment["owned_paths"])
        _git(destination, "apply", "--check", str(patch_file))
        _git(destination, "apply", str(patch_file))
    finally:
        patch_file.unlink(missing_ok=True)
    changed = _git(destination, "status", "--porcelain", "-z", "--untracked-files=all")
    if not changed:
        raise PatchWorkerError("Approved patch made no change")
    # Stage only the paths in the committed brief. Git then gives the final
    # authoritative list, including newly added files and mode changes.
    for path in assignment["owned_paths"]:
        _git(destination, "add", "--", path)
    staged = _git(destination, "diff", "--cached", "--no-renames", "--name-only", "-z")
    paths = sorted(os.fsdecode(value) for value in staged.split(b"\0") if value)
    if paths != planned_paths:
        raise PatchWorkerError("Patch changed paths outside assigned scope")
    if _git(destination, "status", "--porcelain", "-z", "--untracked-files=all").count(b"\0") != len(paths):
        raise PatchWorkerError("Patch left unstaged or unrelated changes")
    for path in paths:
        stage = _git(destination, "ls-files", "--stage", "--", path)
        mode = stage.split(None, 1)[0] if stage else b""
        if mode != b"100644":
            raise PatchWorkerError("Patch creates an unsupported file mode")
        if not (destination / path).is_file() or (destination / path).is_symlink():
            raise PatchWorkerError("Patch deletes or links an assigned file")
        if path.endswith(".py"):
            if (destination / path).stat().st_size > 1_048_576:
                raise PatchWorkerError("Python source exceeds syntax-check bound")
            ast.parse((destination / path).read_bytes(), filename=path)
    _git(destination, "diff", "--cached", "--check")
    _git(destination, "-c", "user.name=SkyBuild CPU Worker", "-c",
         "user.email=cpu-worker@users.noreply.github.com", "commit", "-m",
         "Apply approved patch for " + assignment["task_id"], timeout=60)
    head = _git(destination, "rev-parse", "HEAD").decode().strip()
    if (_git(destination, "rev-list", "--parents", "-n", "1", "HEAD").decode().split() != [head, base]
            or _changed_paths(destination, base) != paths
            or _git(destination, "status", "--porcelain", "--untracked-files=all")):
        raise PatchWorkerError("Committed patch differs from checked source")
    for path in paths:
        if _git(destination, "show", "HEAD:" + path) != (destination / path).read_bytes():
            raise PatchWorkerError("Committed bytes differ from checked file")
    return head, paths


def run(client: Client, *, project: str, worker: str, dispatcher: str, checkout: Path,
        message_id: str, patch_path: Path, patch_sha256: str, state_dir: Path,
        approved_until: datetime, git_token_file: Path) -> dict:
    """Receive, claim, apply, push and submit one pinned assignment exactly once."""
    checkout = checkout.resolve()
    def require_time() -> None:
        if datetime.now(timezone.utc) >= approved_until:
            raise PatchWorkerError("Approval expired; preserve attempt")

    require_time()
    state_dir = _state_directory(state_dir, checkout)
    if any(state_dir.iterdir()):
        raise PatchWorkerError("Attempt directory is not new; reconcile prior effects")
    patch = _patch_bytes(patch_path, patch_sha256)
    inbox = client.inbox(project, limit=100)
    selected = [item for item in inbox if isinstance(item, dict) and item.get("message_id") == message_id]
    if len(selected) != 1:
        raise PatchWorkerError("Expected one pinned worker assignment")
    try:
        assignment = json.loads(selected[0]["body"])
    except (KeyError, TypeError, ValueError):
        raise PatchWorkerError("Assignment body is invalid") from None
    verify_assignment(assignment, checkout, worker=worker)
    if assignment["dispatcher"] != dispatcher:
        raise PatchWorkerError("Assignment dispatcher differs")
    task = client.get_task(project, assignment["task_id"])
    _approved_task(task, assignment, patch_sha256)
    _record(state_dir, "intent.json", {"schema": "skybuild.auto-patch-attempt.v1", "project_id": project,
             "task_id": assignment["task_id"], "assignment_id": assignment["assignment_id"],
             "message_id": message_id, "worker": worker, "patch_sha256": patch_sha256,
             "base_sha": assignment["base_sha"]})
    assignment_path = state_dir / "assignment.json"
    require_time()
    received = receive_assignment(client, project, checkout, worker=worker, dispatcher=dispatcher,
                                  message_id=message_id, destination=assignment_path)
    if received.get("place") != "working":
        raise PatchWorkerError("Worker claim was not confirmed")
    require_time()
    renew_assignment(client, project, assignment, worker, _workflow_path(assignment_path))
    destination = state_dir / "source"
    require_time()
    head, paths = _clone_and_apply(checkout, destination, assignment, patch)
    require_time()
    renew_assignment(client, project, assignment, worker, _workflow_path(assignment_path))
    remote_ref = "refs/heads/" + assignment["branch"]
    if _git(destination, "ls-remote", "origin", remote_ref).strip():
        raise PatchWorkerError("Task branch already exists on origin")
    askpass = _askpass(state_dir, git_token_file)
    _record(state_dir, "push-intent.json", {"head_sha": head, "remote_ref": remote_ref})
    require_time()
    _git(destination, "push", "--atomic", "--force-with-lease=" + remote_ref + ":",
         "origin", "HEAD:" + remote_ref, timeout=120,
         askpass=askpass, git_token_file=git_token_file)
    if _git(destination, "ls-remote", "origin", remote_ref).decode().strip() != f"{head}\t{remote_ref}":
        raise PatchWorkerError("Pushed branch head is unconfirmed")
    result = {"schema": "manual-work-v1", "assignment_id": assignment["assignment_id"],
              "phase": "ready-for-review", "branch": assignment["branch"], "head_sha": head,
              "checks": ["Approved patch SHA-256 matched task metadata", "Python syntax parsed where applicable",
                         "git diff --cached --check passed", "Committed bytes, paths and parent matched"],
              "changed_paths": paths, "risks": [],
              "next_action": "Independent exact-head review, validation and trusted bundle gate"}
    _record(state_dir, "result-intent.json", result)
    require_time()
    submitted = send_result(client, project, checkout, destination, worker=worker,
                            assignment=assignment, result=result,
                            workflow_state=_workflow_path(assignment_path))
    _record(state_dir, "submitted.json", submitted)
    return {"task_id": assignment["task_id"], "worker": worker, "head_sha": head,
            "branch": assignment["branch"], "message_id": submitted["message_id"],
            "attempt_id": received["attempt_id"], "claim_fence": received["claim_fence"],
            "state_dir": str(state_dir), "submitted": True}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("url", "project", "worker", "dispatcher", "message-id", "patch-sha256"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--approved-until", required=True)
    for name in ("checkout", "token-file", "git-token-file", "ca-file", "patch", "state-dir"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        expiry = datetime.fromisoformat(args.approved_until.replace("Z", "+00:00"))
        if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
            raise PatchWorkerError("Approval cutoff is unavailable")
        _private_endpoint(args.url, _resolved_addresses)
        probe_private_api(args.url, args.project, args.token_file, args.worker,
                          ca_file=args.ca_file, workflow=True)
        digest = ca_file_sha256(args.ca_file)
        with Client(args.url, _token_from_file(args.token_file), retries=0, timeout=10,
                    trust_env=False, ca_file=args.ca_file, expected_ca_sha256=digest) as client:
            output = run(client, project=args.project, worker=args.worker, dispatcher=args.dispatcher,
                         checkout=args.checkout, message_id=args.message_id,
                         patch_path=args.patch, patch_sha256=args.patch_sha256,
                         state_dir=args.state_dir, approved_until=expiry,
                         git_token_file=args.git_token_file)
        print(json.dumps(output, sort_keys=True))
        return 0
    except (PatchWorkerError, ClientError, OSError, ValueError, TypeError,
            subprocess.SubprocessError):
        print(json.dumps({"submitted": False, "reason": "Worker stopped; preserve private attempt evidence"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
