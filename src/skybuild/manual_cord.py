"""One-shot REST/Cord transport for the manually assigned worker pilot.

This module does not start a model or claim, reserve, or fence a task. The
dispatcher binds and rechecks an API task status/revision snapshot; that read
does not prevent concurrent assignments. The dispatcher separately reviews
the reported Git head.
"""

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

from .client import Client, ClientError
from .fleet_preflight import PreflightError, _token_from_file, probe_private_api
from .manual_assignment import AssignmentError, verify_assignment


class ManualCordError(ValueError):
    pass


def _git(checkout: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=checkout, capture_output=True, text=True, check=False)
    if result.returncode:
        raise ManualCordError("Worker Git state does not match the assignment")
    return result.stdout.strip()


def _private_write(destination: Path, payload: bytes) -> None:
    """Persist before acknowledging; an interrupted write needs reconciliation."""
    if destination.is_symlink() or not destination.parent.is_dir():
        raise ManualCordError("Assignment destination must have an existing directory")
    if destination.exists():
        descriptor = os.open(destination, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ManualCordError("Existing assignment file is not private")
            if stream.read() != payload:
                raise ManualCordError("Assignment destination contains different evidence")
            os.fsync(stream.fileno())
        directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        # Preserve partial evidence for manual reconciliation.
        raise


def receive_assignment(client: Client, project: str, checkout: Path, *, worker: str,
                       dispatcher: str, message_id: str, destination: Path) -> dict:
    """Save one sender-checked, committed assignment and acknowledge its receipt."""
    messages = client.inbox(project, limit=100, offset=0)
    if not isinstance(messages, list):
        raise ManualCordError("Cord inbox response is invalid")
    matching = [message for message in messages if isinstance(message, dict)
                and message.get("message_id") == message_id]
    if len(matching) != 1:
        raise ManualCordError("Expected one unhandled assignment message in the first inbox page")
    message = matching[0]
    if (message.get("sender") != dispatcher or message.get("recipient") != worker
            or message.get("category") != "manual-work"):
        raise ManualCordError("Assignment sender, recipient or category differs")
    try:
        envelope = json.loads(message["body"])
    except (KeyError, TypeError, ValueError) as error:
        raise ManualCordError("Assignment message body is invalid JSON") from error
    snapshot = verify_assignment(envelope, checkout, worker=worker)
    if envelope["dispatcher"] != dispatcher:
        raise ManualCordError("Assignment dispatcher differs from authenticated sender")
    if envelope["schema"] != "manual-work-v2":
        raise ManualCordError("Legacy assignment needs API-bound redispatch")
    try:
        task = client.get_task(project, envelope["task_id"])
    except ClientError as error:
        raise ManualCordError(f"Assigned task check failed ({error.code})") from None
    if (not isinstance(task, dict) or task.get("task_id") != envelope["task_id"]
            or task.get("status") != envelope["task_status"]
            or type(task.get("revision")) is not int
            or task["revision"] != envelope["task_revision"]):
        raise ManualCordError("Task changed after dispatcher prepared the assignment")
    payload = (json.dumps(envelope, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    _private_write(destination, payload)
    key = "manual-receipt-" + hashlib.sha256(message_id.encode()).hexdigest()
    client.message_action(project, message_id, "receipt", idempotency_key=key)
    return {"saved": str(destination), "message_id": message_id, "assignment_id": snapshot["assignment_id"],
            "base_sha": snapshot["base_sha"], "verified": True,
            "authority": "api", "brief_authority": "git",
            "task_revision": snapshot["task_revision"], "task_status": snapshot["task_status"]}



def send_result(client: Client, project: str, checkout: Path, worktree: Path, *, worker: str,
                assignment: dict, result: dict) -> dict:
    """Send a result tied to a clean owned branch and exact local Git head."""
    snapshot = verify_assignment(assignment, checkout, worker=worker)
    required = {"schema", "assignment_id", "phase", "branch", "head_sha", "checks",
                "changed_paths", "risks", "next_action"}
    if not isinstance(result, dict) or set(result) != required or result.get("schema") != "manual-work-v1":
        raise ManualCordError("Result fields do not match manual-work-v1")
    if (result["assignment_id"] != snapshot["assignment_id"] or
            result["phase"] not in {"in-progress", "blocked", "ready-for-review"} or
            result["branch"] != snapshot["branch"]):
        raise ManualCordError("Result assignment, phase or branch differs")
    for field in ("checks", "changed_paths", "risks"):
        values = result[field]
        if not isinstance(values, list) or len(values) > 100 or any(
                not isinstance(value, str) or not value or len(value) > 500 for value in values):
            raise ManualCordError("Result contains invalid bounded evidence")
    if not isinstance(result["next_action"], str) or not 0 < len(result["next_action"]) <= 500:
        raise ManualCordError("Result needs a bounded next action")
    worktree = worktree.resolve()
    if Path(_git(worktree, "rev-parse", "--show-toplevel")).resolve() != worktree:
        raise ManualCordError("Result must name the exact worktree")
    if _git(worktree, "symbolic-ref", "--short", "HEAD") != snapshot["branch"]:
        raise ManualCordError("Result branch differs from worktree")
    head = _git(worktree, "rev-parse", "HEAD")
    if result["head_sha"] != head or _git(worktree, "status", "--porcelain", "--untracked-files=all"):
        raise ManualCordError("Result must name a clean exact Git head")
    _git(worktree, "merge-base", "--is-ancestor", snapshot["base_sha"], head)
    changed = subprocess.run(["git", "diff", "--no-renames", "--name-only", "-z", snapshot["base_sha"], head],
                             cwd=worktree, capture_output=True, check=False)
    if changed.returncode:
        raise ManualCordError("Result changed paths are unavailable")
    paths = [os.fsdecode(path) for path in changed.stdout.split(b"\0") if path]
    if sorted(paths) != sorted(result["changed_paths"]) or any(
            not any(path == owned or path.startswith(owned + "/") for owned in snapshot["owned_paths"])
            for path in paths):
        raise ManualCordError("Result changed paths differ from assigned scope")
    body = json.dumps(result, sort_keys=True, ensure_ascii=False)
    if len(body.encode("utf-8")) > 32768:
        raise ManualCordError("Result exceeds Cord message limit")
    message = {"recipient": assignment["dispatcher"], "subject": "Manual work result",
               "body": body, "category": "manual-work"}
    key = "manual-result-" + hashlib.sha256((snapshot["assignment_id"] + body).encode()).hexdigest()
    sent = client.send_message(project, message, idempotency_key=key)
    return {"assignment_id": snapshot["assignment_id"], "head_sha": head,
            "message_id": sent.get("message_id"), "sent": True}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--worker", required=True)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--ca-file", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    receive = commands.add_parser("receive", help="Save one verified assignment before receipt")
    receive.add_argument("--dispatcher", required=True)
    receive.add_argument("--message-id", required=True)
    receive.add_argument("--destination", type=Path, required=True)
    report = commands.add_parser("result", help="Send one exact-head result from a clean worktree")
    report.add_argument("--assignment", type=Path, required=True)
    report.add_argument("--result", type=Path, required=True)
    report.add_argument("--worktree", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        probe_private_api(args.url, args.project, args.token_file, args.worker, ca_file=args.ca_file)
        with Client(args.url, _token_from_file(args.token_file), retries=0, trust_env=False,
                    ca_file=args.ca_file) as client:
            if args.command == "receive":
                output = receive_assignment(client, args.project, args.checkout, worker=args.worker,
                                            dispatcher=args.dispatcher, message_id=args.message_id,
                                            destination=args.destination)
            else:
                assignment = json.loads(args.assignment.read_text(encoding="utf-8"))
                result = json.loads(args.result.read_text(encoding="utf-8"))
                output = send_result(client, args.project, args.checkout, args.worktree,
                                     worker=args.worker, assignment=assignment, result=result)
        print(json.dumps(output, sort_keys=True))
        return 0
    except (PreflightError, AssignmentError, ManualCordError, ClientError, OSError, ValueError, TypeError):
        print(json.dumps({"ok": False, "reason": "Manual Cord operation failed; preserve local evidence"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
