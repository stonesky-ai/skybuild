"""Explicit controller compatibility for revision-bound Cord delivery.

Worker validators and worker source remain unchanged. New durable sends use the
exact assignment envelope. Retained sends keep their original key and body.
"""

import fcntl
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Callable, Iterable

from .client import Client, ClientError, ca_file_sha256
from .contracts import valid_identifier
from .fleet_preflight import _resolved_addresses, _token_from_file
from .manual_assignment import AssignmentError, _path, verify_assignment
from .manual_dispatch import (DispatchError, _atomic_json, _private_endpoint,
    _published_head, _read_state, _require_dispatchable_place, _state_directory,
    build_envelope)


def _assignment_key(project: str, envelope: dict) -> str:
    encoded = json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    identity = hashlib.sha256((project + "\0" + encoded).encode()).hexdigest()
    return f"manual-work-v2:{identity}"


def dispatch(repo: Path, brief_path: str, *, worker: str, dispatcher: str, project: str,
             principal: str, url: str, token_file: Path, state_dir: Path,
             ca_file: Path | None = None, base_ref: str = "refs/heads/dev-003",
             resolve: Callable[[str], Iterable[str]] = _resolved_addresses,
             client_factory: Callable[..., Client] = Client,
             expected_envelope: dict | None = None) -> dict:
    """Record intent before I/O; retry only the same Cord body and key."""
    repo = repo.resolve()
    if not valid_identifier(project) or not valid_identifier(principal):
        raise DispatchError("Project or principal identifier is invalid")
    try:
        brief_path = _path(brief_path)
    except AssignmentError as error:
        raise DispatchError(str(error)) from error
    if not brief_path.startswith("docs/design/assignments/") or not brief_path.endswith(".json"):
        raise DispatchError("Brief must be an assignment JSON path")
    if dispatcher != principal:
        raise DispatchError("Dispatcher must equal the authenticated principal")
    endpoint = _private_endpoint(url, resolve)
    ca_sha256 = ca_file_sha256(ca_file) if ca_file is not None else None
    state_dir = _state_directory(state_dir, repo)
    slot = hashlib.sha256(f"{project}\0{brief_path}".encode()).hexdigest()
    path = state_dir / f"{slot}.json"
    lock_path = state_dir / f"{slot}.lock"
    lock_descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(lock_descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise DispatchError("Dispatch lock is unsafe")
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        state = _read_state(path)
        if state is None:
            envelope = build_envelope(repo, brief_path, worker=worker, dispatcher=dispatcher, base_ref=base_ref)
            mode = "new"
        else:
            envelope = state.get("assignment")
            if not isinstance(envelope, dict):
                raise DispatchError("Dispatch state needs manual reconciliation")
            try:
                verify_assignment(envelope, repo, worker=worker)
            except AssignmentError as error:
                raise DispatchError("Pinned assignment no longer verifies") from error
            if envelope["dispatcher"] != dispatcher or envelope["brief_path"] != brief_path:
                raise DispatchError("Dispatch request differs from pinned assignment")
            if envelope.get("schema") == "manual-work-v1" and state.get("status") == "sending":
                raise DispatchError("Legacy sending intent needs manual reconciliation")
            mode = "pinned_retry" if state.get("status") == "sending" else "prepared_retry"
        if expected_envelope is not None:
            expected_initial = {key: value for key, value in expected_envelope.items()
                                if key not in {"task_status", "task_revision"}}
            expected_initial["schema"] = "manual-work-v1"
            if expected_envelope.get("schema") != "manual-work-v2" or envelope not in (expected_initial, expected_envelope):
                raise DispatchError("Selected assignment differs from approved exact envelope")
        identity = hashlib.sha256(f"{project}\0{envelope['assignment_id']}".encode()).hexdigest()
        legacy_key = f"manual-work-v1:{identity}"
        key = _assignment_key(project, envelope) if envelope.get("schema") == "manual-work-v2" else legacy_key
        if state is not None:
            # A retained intent keeps its exact key. Never change an uncertain send.
            if state.get("idempotency_key") not in {legacy_key, key}:
                raise DispatchError("Dispatch key differs from pinned assignment")
            key = state["idempotency_key"]
        body = {"recipient": worker, "subject": f"Manual assignment {envelope['assignment_id']}",
                "body": json.dumps(envelope, sort_keys=True, separators=(",", ":")),
                "category": "manual-work", "urgency": "normal"}
        if len(body["body"]) > 32768 or len(body["subject"]) > 500:
            raise DispatchError("Cord assignment exceeds message size limit")
        intended = {"schema": "manual-dispatch-intent-v1", "project": project, "principal": principal,
                    "endpoint": endpoint, "idempotency_key": key, "message": body, "assignment": envelope}
        if state is None or "base_ref" in state:
            intended["base_ref"] = base_ref
        if ca_sha256 is not None:
            intended["ca_sha256"] = ca_sha256
        if state is None:
            state = {**intended, "status": "prepared", "result": None}
            _atomic_json(path, state)
        elif (state.get("ca_sha256") != ca_sha256
              or any(state.get(field) != value for field, value in intended.items())):
            raise DispatchError("Dispatch request differs from durable intent")
        if state.get("status") == "sent":
            return {"assignment_id": envelope["assignment_id"], "status": "sent",
                    "message_id": state["result"]["message_id"], "state_file": str(path),
                    "mode": "already_sent"}
        if state.get("status") not in {"prepared", "sending"} or state.get("result") is not None:
            raise DispatchError("Dispatch state needs manual reconciliation")
        token = _token_from_file(token_file)
        try:
            with client_factory(url, token, retries=2, timeout=10, trust_env=False,
                                **({"ca_file": ca_file, "expected_ca_sha256": ca_sha256}
                                   if ca_file is not None else {})) as client:
                if client.request("GET", "health/ready") != {"status": "ready"}:
                    raise DispatchError("Private SkyBuild API is not ready")
                identity_response = client.whoami()
                grants = identity_response.get("grants") if isinstance(identity_response, dict) else None
                if (not isinstance(identity_response, dict) or identity_response.get("principal_id") != principal
                        or identity_response.get("is_admin") is not False or not isinstance(grants, dict)
                        or set(grants) != {project} or not isinstance(grants[project], list)
                        or sorted(grants[project]) != ["cord:handle", "cord:read", "cord:send", "tasks:read"]):
                    raise DispatchError("Token does not identify the scoped dispatcher")
                if state["status"] == "prepared":
                    if envelope["schema"] == "manual-work-v1":
                        task = client.get_task(project, envelope["task_id"])
                        if (not isinstance(task, dict) or task.get("task_id") != envelope["task_id"]
                                or not isinstance(task.get("status"), str)
                                or task["status"] not in {"ready", "in-progress"}
                                or type(task.get("revision")) is not int or task["revision"] < 1):
                            raise DispatchError("Task is unavailable or not ready for manual dispatch")
                        _require_dispatchable_place(task)
                        envelope = {**envelope, "schema": "manual-work-v2",
                                    "task_status": task["status"], "task_revision": task["revision"]}
                        if expected_envelope is not None and envelope != expected_envelope:
                            raise DispatchError("Current task differs from approved exact envelope")
                        try:
                            verify_assignment(envelope, repo, worker=worker)
                        except AssignmentError as error:
                            raise DispatchError("Task-bound assignment no longer verifies") from error
                        body = {"recipient": worker, "subject": f"Manual assignment {envelope['assignment_id']}",
                                "body": json.dumps(envelope, sort_keys=True, separators=(",", ":")),
                                "category": "manual-work", "urgency": "normal"}
                        if len(body["body"]) > 32768 or len(body["subject"]) > 500:
                            raise DispatchError("Cord assignment exceeds message size limit")
                        key = _assignment_key(project, envelope)
                        intended = {"schema": "manual-dispatch-intent-v1", "project": project,
                                    "principal": principal, "endpoint": endpoint, "idempotency_key": key,
                                    "message": body, "assignment": envelope}
                        if "base_ref" in state:
                            intended["base_ref"] = state["base_ref"]
                        if ca_sha256 is not None:
                            intended["ca_sha256"] = ca_sha256
                        state = {**intended, "status": "prepared", "result": None}
                        _atomic_json(path, state)
                    else:
                        task = client.get_task(project, envelope["task_id"])
                        if (not isinstance(task, dict) or task.get("task_id") != envelope["task_id"]
                                or task.get("status") != envelope["task_status"]
                                or type(task.get("revision")) is not int
                                or task["revision"] != envelope["task_revision"]):
                            raise DispatchError("Task changed before assignment send")
                        _require_dispatchable_place(task)
                    if mode == "new" and _published_head(repo, base_ref) != envelope["base_sha"]:
                        raise DispatchError("Published development head changed before send")
                    state = {**intended, "status": "sending", "result": None}
                    _atomic_json(path, state)
                result = client.send_message(project, body, idempotency_key=key)
        except ClientError as error:
            raise DispatchError(f"Private SkyBuild API request failed ({error.code})") from None
        if not isinstance(result, dict) or not isinstance(result.get("message_id"), str):
            raise DispatchError("Cord result is invalid; retry with preserved intent")
        state = {**intended, "status": "sent", "result": {"message_id": result["message_id"]}}
        _atomic_json(path, state)
        return {"assignment_id": envelope["assignment_id"], "status": "sent",
                "message_id": result["message_id"], "state_file": str(path), "mode": mode}
    finally:
        os.close(lock_descriptor)


