"""One-shot, pinned manual assignment dispatch through private SkyBuild Cord."""

import argparse
import fcntl
import hashlib
import ipaddress
import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Iterable

import httpx

from .client import Client, ClientError
from .contracts import valid_identifier
from .fleet_preflight import _resolved_addresses, _token_from_file
from .manual_assignment import AssignmentError, _path, verify_assignment


class DispatchError(ValueError):
    pass


def _git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, check=False, timeout=30)
    if result.returncode:
        raise DispatchError("Pinned development revision is unavailable")
    return result.stdout


def _published_head(repo: Path) -> str:
    remote = _git(repo, "ls-remote", "--exit-code", "origin", "refs/heads/dev-002").decode().strip()
    fields = remote.split()
    if len(fields) != 2 or fields[1] != "refs/heads/dev-002":
        raise DispatchError("Published development head is invalid")
    base = fields[0]
    if _git(repo, "rev-parse", "--verify", "refs/remotes/origin/dev-002").decode().strip() != base:
        raise DispatchError("Fetch the published development head before dispatch")
    return base


def build_envelope(repo: Path, brief_path: str, *, worker: str, dispatcher: str) -> dict:
    """Bind a committed brief to the current published development head."""
    if not valid_identifier(worker) or not valid_identifier(dispatcher):
        raise DispatchError("Worker or dispatcher identifier is invalid")
    try:
        brief_path = _path(brief_path)
    except AssignmentError as error:
        raise DispatchError(str(error)) from error
    if not brief_path.startswith("docs/design/assignments/") or not brief_path.endswith(".json"):
        raise DispatchError("Brief must be an assignment JSON path")
    repo = repo.resolve()
    base = _published_head(repo)
    brief = _git(repo, "show", f"{base}:{brief_path}")
    if len(brief) > 65536:
        raise DispatchError("Brief exceeds size limit")
    try:
        document = json.loads(brief.decode("utf-8"))
    except (UnicodeError, ValueError) as error:
        raise DispatchError("Committed brief is not UTF-8 JSON") from error
    if not isinstance(document, dict) or document.get("worker") != worker or document.get("dispatcher") != dispatcher:
        raise DispatchError("Brief names another worker or dispatcher")
    fields = ("assignment_id", "task_id", "worker", "dispatcher", "branch", "owned_paths", "checks", "model_limit")
    try:
        envelope = {"schema": "manual-work-v1", **{field: document[field] for field in fields},
                    "base_sha": base, "brief_path": brief_path, "brief_sha256": hashlib.sha256(brief).hexdigest()}
        verify_assignment(envelope, repo, worker=worker)
    except (KeyError, AssignmentError) as error:
        raise DispatchError("Committed brief does not satisfy manual assignment contract") from error
    return envelope


def _private_endpoint(url: str, resolve: Callable[[str], Iterable[str]]) -> str:
    endpoint = httpx.URL(url)
    if (endpoint.scheme != "https" or not endpoint.host or not endpoint.host.endswith(".ts.net")
            or endpoint.userinfo or endpoint.query or endpoint.fragment or endpoint.path not in {"", "/"}):
        raise DispatchError("Use the private Tailscale HTTPS service root")
    try:
        addresses = [ipaddress.ip_address(address) for address in resolve(endpoint.host)]
    except (OSError, ValueError) as error:
        raise DispatchError("Private service address is unavailable") from error
    v4 = ipaddress.ip_network("100.64.0.0/10")
    v6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
    if not addresses or any(address not in (v4 if address.version == 4 else v6) for address in addresses):
        raise DispatchError("Service hostname must resolve only to Tailscale addresses")
    return str(endpoint)


def _state_directory(path: Path, repo: Path) -> Path:
    if not path.is_absolute() or path != path.resolve() or path == repo or repo in path.parents:
        raise DispatchError("State directory must be absolute and outside the checkout")
    if path.is_symlink():
        raise DispatchError("State directory must not be a symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise DispatchError("State directory must be private and owned by this user")
    return path


def _atomic_json(path: Path, value: dict) -> None:
    data = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(data) > 131072:
        raise DispatchError("Dispatch intent exceeds size limit")
    descriptor, temporary = tempfile.mkstemp(prefix=".manual-dispatch-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_state(path: Path) -> dict | None:
    if not path.exists() and not path.is_symlink():
        return None
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_size > 131072:
            raise DispatchError("Dispatch state file is unsafe")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            state = json.load(stream)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(state, dict):
        raise DispatchError("Dispatch state is invalid")
    return state


def dispatch(repo: Path, brief_path: str, *, worker: str, dispatcher: str, project: str,
             principal: str, url: str, token_file: Path, state_dir: Path,
             resolve: Callable[[str], Iterable[str]] = _resolved_addresses,
             client_factory: Callable[..., Client] = Client) -> dict:
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
            envelope = build_envelope(repo, brief_path, worker=worker, dispatcher=dispatcher)
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
            mode = "pinned_retry" if state.get("status") == "sending" else "prepared_retry"
        identity = hashlib.sha256(f"{project}\0{envelope['assignment_id']}".encode()).hexdigest()
        key = f"manual-work-v1:{identity}"
        body = {"recipient": worker, "subject": f"Manual assignment {envelope['assignment_id']}",
                "body": json.dumps(envelope, sort_keys=True, separators=(",", ":")),
                "category": "manual-work", "urgency": "normal"}
        if len(body["body"]) > 32768 or len(body["subject"]) > 500:
            raise DispatchError("Cord assignment exceeds message size limit")
        intended = {"schema": "manual-dispatch-intent-v1", "project": project, "principal": principal,
                    "endpoint": endpoint, "idempotency_key": key, "message": body, "assignment": envelope}
        if state is None:
            state = {**intended, "status": "prepared", "result": None}
            _atomic_json(path, state)
        elif any(state.get(field) != value for field, value in intended.items()):
            raise DispatchError("Dispatch request differs from durable intent")
        if state.get("status") == "sent":
            return {"assignment_id": envelope["assignment_id"], "status": "sent",
                    "message_id": state["result"]["message_id"], "state_file": str(path),
                    "mode": "already_sent"}
        if state.get("status") not in {"prepared", "sending"} or state.get("result") is not None:
            raise DispatchError("Dispatch state needs manual reconciliation")
        token = _token_from_file(token_file)
        try:
            with client_factory(url, token, retries=2, timeout=10, trust_env=False) as client:
                if client.request("GET", "health/ready") != {"status": "ready"}:
                    raise DispatchError("Private SkyBuild API is not ready")
                identity_response = client.whoami()
                grants = identity_response.get("grants") if isinstance(identity_response, dict) else None
                if (not isinstance(identity_response, dict) or identity_response.get("principal_id") != principal
                        or identity_response.get("is_admin") is not False or not isinstance(grants, dict)
                        or set(grants) != {project} or not isinstance(grants[project], list)
                        or sorted(grants[project]) != ["cord:handle", "cord:read", "cord:send"]):
                    raise DispatchError("Token does not identify the scoped dispatcher")
                if state["status"] == "prepared":
                    if mode == "new" and _published_head(repo) != envelope["base_sha"]:
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--brief-path", required=True)
    parser.add_argument("--worker", required=True)
    parser.add_argument("--dispatcher", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--principal", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(dispatch(args.checkout, args.brief_path, worker=args.worker,
                                  dispatcher=args.dispatcher, project=args.project, principal=args.principal,
                                  url=args.url, token_file=args.token_file, state_dir=args.state_dir), sort_keys=True))
        return 0
    except (DispatchError, AssignmentError, OSError, ValueError, UnicodeError, subprocess.TimeoutExpired) as error:
        reason = str(error) if isinstance(error, DispatchError) else "Dispatch input or environment is invalid"
        print(json.dumps({"status": "blocked", "reason": reason}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
