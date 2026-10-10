"""One-shot REST/Cord transport for the manually assigned worker pilot.

This module starts no model. Petri assignments acquire a fenced API claim;
legacy assignments retain their read-only status/revision check. A submitted
head is proposed work. The dispatcher separately reviews the reported head.
"""

import argparse
from datetime import datetime, timezone
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
from .manual_dispatch import _read_state


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
    if not isinstance(task, dict) or task.get("task_id") != envelope["task_id"]:
        raise ManualCordError("Assigned task response differs")
    petri = _petri_token(task)
    claim_state = None
    if petri is not None:
        claim_state = _claim_assignment(client, project, envelope, task, destination, worker)
    elif (task.get("status") != envelope["task_status"]
            or type(task.get("revision")) is not int
            or task["revision"] != envelope["task_revision"]):
        raise ManualCordError("Task changed after dispatcher prepared the assignment")
    payload = (json.dumps(envelope, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    _private_write(destination, payload)
    key = "manual-receipt-" + hashlib.sha256(message_id.encode()).hexdigest()
    client.message_action(project, message_id, "receipt", idempotency_key=key)
    response = {"saved": str(destination), "message_id": message_id, "assignment_id": snapshot["assignment_id"],
            "base_sha": snapshot["base_sha"], "verified": True,
            "authority": "api", "brief_authority": "git",
            "task_revision": snapshot["task_revision"], "task_status": snapshot["task_status"]}
    if claim_state is not None:
        response.update(place="working", workflow_state=str(_workflow_path(destination)),
                        claim_fence=claim_state["token"]["claim_fence"], attempt_id=claim_state["token"]["attempt_id"])
    return response


def _petri_token(task):
    petri = task.get("metadata", {}).get("_skybuild_workflow", {}).get("petri")
    return petri.get("token") if isinstance(petri, dict) and petri.get("schema_version") == 1 else None


def _workflow_path(assignment_path):
    return assignment_path.with_name(assignment_path.name + ".workflow.json")


def _claim_assignment(client, project, assignment, task, destination, worker):
    """Persist a stable claim request before I/O and retain its exact binding."""
    fingerprint = hashlib.sha256(json.dumps(assignment, sort_keys=True).encode()).hexdigest()
    intent = {"schema": "manual-petri-claim-v1", "project_id": project, "worker": worker,
              "assignment_id": assignment["assignment_id"], "task_id": assignment["task_id"],
              "expected_revision": assignment["task_revision"], "assignment_sha256": fingerprint}
    path = _workflow_path(destination)
    intent_path = path.with_name(path.name + ".intent")
    existing = _read_state(intent_path)
    if existing is None:
        token = _petri_token(task)
        if (task.get("revision") != assignment["task_revision"] or token.get("place") != "ready"
                or token.get("pending_action") is not None or token.get("superseded")):
            raise ManualCordError("Petri assignment is no longer permitted Ready work")
        _private_write(intent_path, (json.dumps(intent, sort_keys=True) + "\n").encode())
    elif existing != intent:
        raise ManualCordError("Assignment differs from the durable claim intent")
    key = "manual-claim-" + hashlib.sha256((project + "\n" + assignment["assignment_id"]).encode()).hexdigest()
    claim = client.claim_task(project, assignment["task_id"], expected_revision=intent["expected_revision"],
                              idempotency_key=key, lease_seconds=300)
    view = client.task_workflow(project, assignment["task_id"])
    token = view.get("token") if isinstance(view, dict) else None
    if (not isinstance(claim, dict) or not isinstance(token, dict) or claim.get("holder") != worker
            or claim.get("held") is not True or type(claim.get("fence")) is not int
            or token.get("claim_fence") != claim["fence"] or not token.get("attempt_id")
            or token.get("place") != "working" or token.get("pending_action") is not None
            or token.get("project_id") != project or token.get("task_id") != assignment["task_id"]
            or token.get("revision") != claim.get("task_revision")):
        raise ManualCordError("Claim binding changed; preserve the durable intent and reconcile")
    try:
        lease_until = datetime.fromisoformat(claim["lease_until"].replace("Z", "+00:00"))
        if lease_until.tzinfo is None or lease_until <= datetime.now(timezone.utc):
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ManualCordError("Assignment claim lease is not current; reconcile ownership") from None
    state = {**intent, "token": token, "claim": claim}
    _private_write(path, (json.dumps(state, sort_keys=True) + "\n").encode())
    return state



def send_result(client: Client, project: str, checkout: Path, worktree: Path, *, worker: str,
                assignment: dict, result: dict, workflow_state: Path | None = None) -> dict:
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
    if workflow_state is not None:
        _submit_result(client, project, assignment, result, worker, workflow_state, key)
    elif _petri_token(client.get_task(project, assignment["task_id"])) is not None:
        raise ManualCordError("Petri result requires the saved fenced assignment binding")
    sent = client.send_message(project, message, idempotency_key=key)
    return {"assignment_id": snapshot["assignment_id"], "head_sha": head,
            "message_id": sent.get("message_id"), "sent": True}


def _submit_result(client, project, assignment, result, worker, path, key):
    state = _read_state(path)
    if state is None:
        task = client.get_task(project, assignment["task_id"])
        if _petri_token(task) is not None:
            raise ManualCordError("Petri result requires the saved fenced assignment binding")
        return
    fingerprint = hashlib.sha256(json.dumps(assignment, sort_keys=True).encode()).hexdigest()
    if (state.get("schema") != "manual-petri-claim-v1" or state.get("project_id") != project
            or state.get("worker") != worker or state.get("assignment_id") != assignment["assignment_id"]
            or state.get("task_id") != assignment["task_id"] or state.get("assignment_sha256") != fingerprint):
        raise ManualCordError("Result differs from the saved fenced assignment")
    token = state["token"]
    receipt = {name: token[name] for name in ("attempt_id", "claim_fence", "input_generation",
                                             "definition_revision", "policy_version")}
    receipt.update(source_head=result["head_sha"], source_branch="refs/heads/" + assignment["branch"],
                   target_base=token.get("target_base") or assignment["base_sha"])
    intent_path = path.with_name(path.name + ".submit")
    intent = _read_state(intent_path)
    if result["phase"] != "ready-for-review":
        if intent is not None:
            raise ManualCordError("A submitted attempt cannot report more author work")
        _current_assignment(client, project, assignment, token)
        return
    if intent is None:
        current = _current_assignment(client, project, assignment, token)
        intent = {"body": receipt, "expected_revision": current["revision"], "idempotency_key": key}
        _private_write(intent_path, (json.dumps(intent, sort_keys=True) + "\n").encode())
    elif intent.get("body") != receipt or intent.get("idempotency_key") != key:
        raise ManualCordError("Result differs from the durable submission intent")
    outcome = client.workflow_transition(project, assignment["task_id"], "submit", intent["body"],
                                         expected_revision=intent["expected_revision"],
                                         idempotency_key=intent["idempotency_key"])
    if not isinstance(outcome, dict) or outcome.get("token", {}).get("place") != "validating":
        raise ManualCordError("Task submission is unconfirmed; preserve the durable intent")


def _current_assignment(client, project, assignment, token):
    view = client.task_workflow(project, assignment["task_id"])
    current = view.get("token") if isinstance(view, dict) else None
    if (not isinstance(current, dict) or current.get("place") != "working"
            or current.get("pending_action") is not None or
            any(current.get(name) != token.get(name) for name in
                ("project_id", "task_id", "attempt_id", "claim_fence", "input_generation",
                 "definition_revision", "policy_version", "source_head", "target_base"))):
        raise ManualCordError("Result attempt or inputs are stale")
    return current


def renew_assignment(client, project, assignment, worker, path):
    """Renew one current claim. An expired lease requires reconciliation."""
    state = _read_state(path)
    fingerprint = hashlib.sha256(json.dumps(assignment, sort_keys=True).encode()).hexdigest()
    if (state is None or state.get("project_id") != project or state.get("worker") != worker
            or state.get("assignment_sha256") != fingerprint):
        raise ManualCordError("Renewal requires the saved fenced assignment binding")
    current = _current_assignment(client, project, assignment, state["token"])
    from uuid import uuid4
    claim = client.request("POST", Client._path(project, "tasks/" + Client._segment(assignment["task_id"]) + "/claim/renew"),
                           body={"fence": current["claim_fence"], "lease_seconds": 300},
                           revision=current["revision"], idempotency_key="manual-renew-" + uuid4().hex)
    return {"assignment_id": assignment["assignment_id"], "claim_fence": claim["fence"],
            "lease_until": claim["lease_until"], "renewed": True}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--worker", required=True)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--ca-file", type=Path)
    parser.add_argument("--workflow", action="store_true", help="Use the explicit Petri worker credential profile")
    commands = parser.add_subparsers(dest="command", required=True)
    receive = commands.add_parser("receive", help="Save one verified assignment before receipt")
    receive.add_argument("--dispatcher", required=True)
    receive.add_argument("--message-id", required=True)
    receive.add_argument("--destination", type=Path, required=True)
    report = commands.add_parser("result", help="Send one exact-head result from a clean worktree")
    report.add_argument("--assignment", type=Path, required=True)
    report.add_argument("--result", type=Path, required=True)
    report.add_argument("--worktree", type=Path, required=True)
    renewal = commands.add_parser("renew", help="Renew one current Petri assignment claim")
    renewal.add_argument("--assignment", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        probe_private_api(args.url, args.project, args.token_file, args.worker, ca_file=args.ca_file,
                          **({"workflow": True} if args.workflow else {}))
        with Client(args.url, _token_from_file(args.token_file), retries=0, trust_env=False,
                    ca_file=args.ca_file) as client:
            if args.command == "receive":
                output = receive_assignment(client, args.project, args.checkout, worker=args.worker,
                                            dispatcher=args.dispatcher, message_id=args.message_id,
                                            destination=args.destination)
            elif args.command == "renew":
                assignment = json.loads(args.assignment.read_text(encoding="utf-8"))
                output = renew_assignment(client, args.project, assignment, args.worker, _workflow_path(args.assignment))
            else:
                assignment = json.loads(args.assignment.read_text(encoding="utf-8"))
                result = json.loads(args.result.read_text(encoding="utf-8"))
                output = send_result(client, args.project, args.checkout, args.worktree,
                                     worker=args.worker, assignment=assignment, result=result,
                                     workflow_state=_workflow_path(args.assignment))
        print(json.dumps(output, sort_keys=True))
        return 0
    except (PreflightError, AssignmentError, ManualCordError, ClientError, OSError, ValueError, TypeError):
        print(json.dumps({"ok": False, "reason": "Manual Cord operation failed; preserve local evidence"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
