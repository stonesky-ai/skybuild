"""Select and launch at most two approved CPU patch workers from Ready tasks.

This one-shot controller uses the existing scoped dispatcher, committed briefs,
Cord transport and atomic task claims. It never receives a publisher credential.
An interrupted dispatch or launch retains its private run directory and needs
observation before any new attempt.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from .auto_patch_worker import _approved_task, _patch_bytes
from .client import Client, ClientError, ca_file_sha256
from .fleet_preflight import _token_from_file, _resolved_addresses
from .manual_dispatch import (DispatchError, _private_endpoint, _state_directory,
                              build_envelope, dispatch)


class AutoControllerError(ValueError):
    pass


def _read_manifest(path: Path) -> list[dict]:
    if path.stat().st_size > 16384:
        raise AutoControllerError("Candidate manifest exceeds 16 KiB")
    data = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or data.get("schema") != "skybuild.auto-patch-candidates.v1"
            or set(data) != {"schema", "candidates"} or not isinstance(data["candidates"], list)
            or not 2 <= len(data["candidates"]) <= 20):
        raise AutoControllerError("Candidate manifest needs 2..20 bounded entries")
    for item in data["candidates"]:
        if (not isinstance(item, dict) or set(item) != {"worker", "brief_path", "patch",
                                                    "patch_sha256", "token_file", "git_token_file"}
                or not isinstance(item["worker"], str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", item["worker"])
                or not isinstance(item["brief_path"], str)
                or not isinstance(item["patch"], str)
                or not isinstance(item["patch_sha256"], str)
                or not isinstance(item["token_file"], str)
                or not isinstance(item["git_token_file"], str)):
            raise AutoControllerError("Candidate entry is invalid")
    return data["candidates"]


def select(client: Client, repo: Path, candidates: list[dict], *, project: str,
           dispatcher: str, base_ref: str) -> list[dict]:
    """Pick two highest-priority distinct Ready tasks and worker principals."""
    identity = client.whoami()
    grants = identity.get("grants") if isinstance(identity, dict) else None
    if (not isinstance(identity, dict) or identity.get("principal_id") != dispatcher
            or identity.get("is_admin") is not False or not isinstance(grants, dict)
            or set(grants) != {project} or not isinstance(grants[project], list)
            or sorted(grants[project]) != ["cord:handle", "cord:read", "cord:send", "tasks:read"]):
        raise AutoControllerError("Controller requires the exact scoped dispatcher")
    choices = []
    for entry in candidates:
        envelope = build_envelope(repo, entry["brief_path"], worker=entry["worker"],
                                  dispatcher=dispatcher, base_ref=base_ref)
        task = client.get_task(project, envelope["task_id"])
        if not isinstance(task, dict) or task.get("status") != "ready":
            continue
        pinned = {**envelope, "task_status": task["status"], "task_revision": task.get("revision")}
        try:
            _approved_task(task, pinned, entry["patch_sha256"])
        except ValueError:
            continue
        _patch_bytes(Path(entry["patch"]), entry["patch_sha256"])
        priority = task.get("priority")
        if type(priority) is not int or not -(2**31) <= priority < 2**31:
            raise AutoControllerError("Ready task priority is invalid")
        choices.append({**entry, "task_id": envelope["task_id"], "assignment_id": envelope["assignment_id"],
                        "branch": envelope["branch"], "owned_paths": envelope["owned_paths"],
                        "priority": priority, "revision": task["revision"], "base_sha": envelope["base_sha"]})
    choices.sort(key=lambda item: (item["priority"], item["task_id"], item["worker"]))
    selected = []
    for item in choices:
        if any(item["worker"] == prior["worker"] or item["task_id"] == prior["task_id"]
               or item["branch"] == prior["branch"] or item["assignment_id"] == prior["assignment_id"]
               or any(a == b or a.startswith(b + "/") or b.startswith(a + "/")
                      for a in item["owned_paths"] for b in prior["owned_paths"])
               for prior in selected):
            continue
        selected.append(item)
        if len(selected) == 2:
            return selected
    raise AutoControllerError("Fewer than two distinct approved Ready assignments")


def _save_new(path: Path, value: dict) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def run(*, repo: Path, manifest: Path, project: str, dispatcher: str, url: str,
        dispatcher_token: Path, ca_file: Path, base_ref: str, state_dir: Path,
        approved_until: str) -> dict:
    expiry = datetime.fromisoformat(approved_until.replace("Z", "+00:00"))
    if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
        raise AutoControllerError("Approval cutoff must be future and offset-aware")
    _private_endpoint(url, _resolved_addresses)
    repo = repo.resolve()
    state_dir = _state_directory(state_dir, repo)
    if any(state_dir.iterdir()):
        raise AutoControllerError("Run directory exists; reconcile before another launch")
    candidates = _read_manifest(manifest)
    ca_digest = ca_file_sha256(ca_file)
    with Client(url, _token_from_file(dispatcher_token), retries=0, timeout=10,
                trust_env=False, ca_file=ca_file, expected_ca_sha256=ca_digest) as client:
        selected = select(client, repo, candidates, project=project,
                          dispatcher=dispatcher, base_ref=base_ref)
    _save_new(state_dir / "selection.json", {"schema": "skybuild.auto-patch-selection.v1",
              "project_id": project, "base_ref": base_ref, "approved_until": approved_until,
              "selected": [{key: item[key] for key in ("task_id", "assignment_id", "worker", "branch",
                                                     "priority", "revision", "base_sha", "patch_sha256")}
                           for item in selected]})
    delivered = []
    for item in selected:
        if datetime.now(timezone.utc) >= expiry:
            raise AutoControllerError("Approval expired before dispatch")
        response = dispatch(repo, item["brief_path"], worker=item["worker"], dispatcher=dispatcher,
                            project=project, principal=dispatcher, url=url, token_file=dispatcher_token,
                            state_dir=state_dir / "dispatch", ca_file=ca_file, base_ref=base_ref)
        delivered.append({**item, "message_id": response["message_id"]})
    _save_new(state_dir / "delivered.json", {"messages": [
        {"task_id": item["task_id"], "worker": item["worker"], "message_id": item["message_id"]}
        for item in delivered]})
    processes = []
    for item in delivered:
        if datetime.now(timezone.utc) >= expiry:
            raise AutoControllerError("Approval expired before worker launch")
        worker_dir = state_dir / ("worker-" + item["worker"])
        if worker_dir.exists() or worker_dir.is_symlink():
            raise AutoControllerError("Worker attempt directory already exists")
        arguments = [sys.executable, "-m", "skybuild.auto_patch_worker", "--url", url,
                     "--project", project, "--worker", item["worker"], "--dispatcher", dispatcher,
                     "--message-id", item["message_id"], "--checkout", str(repo),
                     "--token-file", item["token_file"],
                     "--git-token-file", item["git_token_file"], "--ca-file", str(ca_file),
                     "--patch", item["patch"], "--patch-sha256", item["patch_sha256"],
                     "--state-dir", str(worker_dir), "--approved-until", approved_until]
        _save_new(state_dir / ("worker-" + item["worker"] + ".launch-intent.json"),
                  {"task_id": item["task_id"], "worker": item["worker"],
                   "message_id": item["message_id"], "state_dir": str(worker_dir)})
        environment = {name: os.environ[name] for name in ("HOME", "PATH", "LANG", "LC_ALL") if name in os.environ}
        environment["PYTHONPATH"] = str(repo / "src")
        with (state_dir / ("worker-" + item["worker"] + ".log")).open("xb") as log:
            os.fchmod(log.fileno(), 0o600)
            child = subprocess.Popen(arguments, cwd=repo, env=environment,
                                     stdout=log, stderr=log, start_new_session=True)
        processes.append((item, child))
        _save_new(state_dir / ("worker-" + item["worker"] + ".launch.json"),
                  {"task_id": item["task_id"], "worker": item["worker"], "pid": child.pid,
                   "message_id": item["message_id"]})
    results = []
    for item, child in processes:
        remaining = (expiry - datetime.now(timezone.utc)).total_seconds()
        try:
            code = child.wait(timeout=max(0, remaining))
        except subprocess.TimeoutExpired:
            # Exact child remains owned; no blind retry or broad process kill.
            code = None
        results.append({"task_id": item["task_id"], "worker": item["worker"],
                        "pid": child.pid, "exit_code": code,
                        "log": str(state_dir / ("worker-" + item["worker"] + ".log"))})
    return {"schema": "skybuild.auto-patch-run.v1", "selected": len(selected),
            "workers": results, "state_dir": str(state_dir),
            "submitted": all(item["exit_code"] == 0 for item in results)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("url", "project", "dispatcher", "base-ref", "approved-until"):
        parser.add_argument("--" + name, required=True)
    for name in ("checkout", "manifest", "dispatcher-token", "ca-file", "state-dir"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = run(repo=args.checkout, manifest=args.manifest, project=args.project,
                     dispatcher=args.dispatcher, url=args.url, dispatcher_token=args.dispatcher_token,
                     ca_file=args.ca_file, base_ref=args.base_ref, state_dir=args.state_dir,
                     approved_until=args.approved_until)
        print(json.dumps(result, sort_keys=True))
        return 0 if result["submitted"] else 2
    except (AutoControllerError, DispatchError, ClientError, OSError, ValueError,
            TypeError, subprocess.SubprocessError):
        print(json.dumps({"submitted": False, "reason": "Controller stopped; preserve private run evidence"}),
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
