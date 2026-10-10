"""One-shot, permit-bound delivery of two approved deterministic CPU patches.

The controller selects Ready tasks itself, durably dispatches and claims each
assignment, and launches distinct bounded systemd attempt units. `--resume`
reconstructs only pinned handles for reconciliation; it never dispatches or starts.
"""

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

from scripts.skybuild_job_unit import JobUnitError, JobUnitManager

from .auto_patch_permit import (PermitError, check_source, check_weekly_usage,
                                load as load_permit, resource_admission)
from .auto_patch_worker import (_approved_task, _changed_paths, _git, _patch_bytes,
                               _patch_paths, PatchWorkerError)
from .client import Client, ClientError, ca_file_sha256
from .fleet_preflight import PreflightError, _token_from_file, _resolved_addresses, probe_private_api
from .manual_cord import ManualCordError, receive_assignment, result_message
from .cpu_worker_bridge import (CPUWorkerBridgeError, CPUWorkerPlan, launch_worker,
                                prepare_worker, reconcile_worker, recover_worker,
                                _docker_container_state, _file_bytes, _write_exclusive)
from .manual_dispatch import (DispatchError, _private_endpoint, _state_directory,
                              build_envelope, dispatch)


class AutoControllerError(ValueError):
    pass


def _read_json(path: Path, *, maximum: int = 32768) -> dict:
    data = _file_bytes(path, limit=maximum, private=True)
    try:
        value = json.loads(data)
    except (UnicodeError, ValueError):
        raise AutoControllerError("Worker evidence is malformed") from None
    if not isinstance(value, dict):
        raise AutoControllerError("Worker evidence is malformed")
    return value


def _controller_git(repo: Path, *args: str, timeout: int = 30,
                    authenticated: bool = False) -> bytes:
    if any(name.startswith("GIT_") and name != "GIT_PAGER" for name in os.environ):
        raise AutoControllerError("Inherited Git environment requires reconciliation")
    environment = {name: os.environ[name] for name in ("HOME", "PATH", "LANG", "LC_ALL")
                   if name in os.environ}
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null",
                       GIT_TERMINAL_PROMPT="0", GIT_LFS_SKIP_SMUDGE="1")
    command = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.attributesFile=/dev/null",
               "-c", "core.autocrlf=false"]
    if authenticated:
        if shutil.which("gh", path=environment.get("PATH")) is None:
            raise AutoControllerError("Controller Git credential helper is unavailable")
        command.extend(("-c", "credential.helper=", "-c",
                        "credential.helper=!gh auth git-credential"))
    try:
        result = subprocess.run([*command, *args], cwd=repo, env=environment,
                                capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise AutoControllerError("Controller Git outcome is unknown; preserve run evidence") from None
    if result.returncode:
        raise AutoControllerError("Controller Git operation failed; preserve run evidence")
    return result.stdout


def _prepare_worker_source(repo: Path, destination: Path, base_sha: str) -> Path:
    """Create a clean, credential-free source mount at the task's pinned base."""
    if destination.exists() or destination.is_symlink():
        raise AutoControllerError("Worker source mount exists; reconcile before reuse")
    _git(None, "clone", "--no-local", "--no-checkout", str(repo), str(destination), timeout=120)
    _git(destination, "switch", "--detach", base_sha)
    origin = "https://github.com/stonesky-ai/skybuild.git"
    _git(destination, "remote", "set-url", "origin", origin)
    _git(destination, "remote", "set-url", "--push", "origin", origin)
    if (_git(destination, "rev-parse", "HEAD").decode().strip() != base_sha
            or _git(destination, "status", "--porcelain", "--untracked-files=all")
            or _git(destination, "remote", "get-url", "origin").decode().strip() != origin
            or _git(destination, "remote", "get-url", "--push", "origin").decode().strip() != origin):
        raise AutoControllerError("Worker source mount differs from the pinned clean source")
    destination.chmod(0o700)
    return destination


def _publish_worker_output(prepared, record: dict) -> dict:
    """Verify exact patch output, create-only push it, and stage the bridge result."""
    plan = prepared.plan
    assignment = _read_json(plan.assignment_dir / "assignment.json")
    preclaim = _read_json(plan.assignment_dir / "preclaim.json")
    output = plan.worker_output_dir
    terminal = _read_json(output / "terminal-result.json")
    result = _read_json(output / "result-intent.json")
    try:
        log_bytes = Path(record["log"]).read_bytes()
        if len(log_bytes) > 32768 or len(log_bytes.splitlines()) != 1:
            raise ValueError
        logged = json.loads(log_bytes.decode("utf-8"))
    except (KeyError, OSError, UnicodeError, ValueError):
        raise AutoControllerError("Worker terminal output is unavailable or malformed") from None
    if (logged != terminal or terminal.get("schema") != "skybuild.auto-patch-worker-result.v1"
            or terminal.get("task_id") != prepared.task_id
            or terminal.get("assignment_id") != prepared.assignment_id
            or terminal.get("worker") != plan.worker_id
            or terminal.get("attempt_id") != prepared.attempt_id
            or terminal.get("claim_fence") != prepared.claim_fence
            or terminal.get("patch_sha256") != plan.patch_digest
            or terminal.get("base_sha") != assignment.get("base_sha")
            or terminal.get("branch") != assignment.get("branch")
            or terminal.get("result") != result
            or result.get("assignment_id") != prepared.assignment_id
            or result.get("head_sha") != terminal.get("head_sha")
            or result.get("phase") != "ready-for-review"):
        raise AutoControllerError("Worker terminal identity differs from its fenced assignment")
    source = output / "source"
    head = terminal["head_sha"]
    if (not re.fullmatch(r"[0-9a-f]{40}", str(head))
            or _git(source, "remote", "get-url", "origin").decode().strip()
            != "https://github.com/stonesky-ai/skybuild.git"
            or _git(source, "symbolic-ref", "--short", "HEAD").decode().strip() != assignment["branch"]
            or _git(source, "rev-parse", "HEAD").decode().strip() != head
            or _git(source, "rev-list", "--parents", "-n", "1", "HEAD").decode().split()
            != [head, assignment["base_sha"]]
            or _git(source, "status", "--porcelain", "--untracked-files=all")):
        raise AutoControllerError("Worker commit differs from its clean pinned branch")
    changed = _changed_paths(source, assignment["base_sha"])
    owned = sorted(assignment.get("owned_paths", []))
    if changed != owned or changed != sorted(terminal.get("changed_paths", [])):
        raise AutoControllerError("Worker commit changed files outside the approved scope")
    patch = _patch_bytes(plan.patch_file, plan.patch_digest)
    with tempfile.TemporaryDirectory(prefix="skybuild-cpu-verify-", dir=plan.external_state_dir) as name:
        expected = Path(name) / "expected"
        _git(None, "clone", "--no-local", "--no-checkout", str(plan.checkout), str(expected), timeout=120)
        _git(expected, "switch", "--detach", assignment["base_sha"])
        patch_file = Path(name) / "approved.patch"
        patch_file.write_bytes(patch)
        patch_file.chmod(0o400)
        paths = _patch_paths(expected, assignment["base_sha"], patch_file, owned)
        if paths != changed:
            raise AutoControllerError("Approved patch paths differ from worker output")
        _git(expected, "apply", "--check", str(patch_file))
        _git(expected, "apply", str(patch_file))
        _git(expected, "add", "--", *paths)
        if _git(source, "rev-parse", "HEAD^{tree}").decode().strip() != _git(
                expected, "write-tree").decode().strip():
            raise AutoControllerError("Worker tree differs from the exact approved patch")

    remote_ref = "refs/heads/" + assignment["branch"]
    if (not remote_ref.startswith("refs/heads/task/")
            or _controller_git(None, "check-ref-format", remote_ref).strip()):
        raise AutoControllerError("Approved task branch ref is invalid")
    push_repo = plan.external_state_dir / "controller-push.git"
    if push_repo.is_symlink():
        raise AutoControllerError("Controller publication workspace is unsafe")
    if not push_repo.exists():
        _git(None, "init", "--bare", "--quiet", str(push_repo))
    elif _git(push_repo, "rev-parse", "--is-bare-repository").decode().strip() != "true":
        raise AutoControllerError("Saved controller publication workspace is not bare")
    imported = True
    try:
        _git(push_repo, "cat-file", "-e", head + "^{commit}")
    except PatchWorkerError:
        imported = False
    if not imported:
        _git(push_repo, "fetch", "--no-tags", "--no-recurse-submodules", str(source),
             "refs/heads/" + assignment["branch"])
    try:
        _git(push_repo, "cat-file", "-e", head + "^{commit}")
    except PatchWorkerError:
        raise AutoControllerError("Controller import does not contain the verified commit") from None
    if (_git(push_repo, "rev-parse", "FETCH_HEAD").decode().strip() != head
            or _git(push_repo, "rev-list", "--parents", "-n", "1", head).decode().split()
            != [head, assignment["base_sha"]]):
        raise AutoControllerError("Controller import differs from the verified worker commit")

    remote = "https://github.com/stonesky-ai/skybuild.git"
    existing = _controller_git(push_repo, "ls-remote", remote, remote_ref,
                               authenticated=True).decode().strip()
    push_intent_path = plan.assignment_dir / "push-intent.json"
    intent = {"schema": "skybuild.controller-git-push.v1",
              "operation_id": prepared.operation_id, "task_id": prepared.task_id,
              "assignment_id": prepared.assignment_id, "worker": plan.worker_id,
              "attempt_id": prepared.attempt_id, "claim_fence": prepared.claim_fence,
              "remote_ref": remote_ref, "head_sha": head, "base_sha": assignment["base_sha"],
              "patch_sha256": plan.patch_digest}
    if push_intent_path.exists() or push_intent_path.is_symlink():
        if _read_json(push_intent_path, maximum=8192) != intent:
            raise AutoControllerError("Saved push intent differs; preserve dispatch evidence")
        # Existing intent means a write may have happened. Reconcile only.
        if existing != head + "\t" + remote_ref:
            raise AutoControllerError("Prior push outcome remains unknown; never retry the push")
    else:
        if existing:
            raise AutoControllerError("Task branch already exists; preserve dispatch evidence")
        _save_new(push_intent_path, intent)
        try:
            _controller_git(push_repo, "push", "--atomic",
                            "--force-with-lease=" + remote_ref + ":", remote,
                            head + ":" + remote_ref, timeout=120, authenticated=True)
        except AutoControllerError:
            confirmed_after_uncertain = _controller_git(
                push_repo, "ls-remote", remote, remote_ref, authenticated=True).decode().strip()
            if confirmed_after_uncertain != head + "\t" + remote_ref:
                raise AutoControllerError("Task branch push outcome is unknown; preserve run evidence") from None
            existing = confirmed_after_uncertain
    confirmed = _controller_git(push_repo, "ls-remote", remote, remote_ref,
                                authenticated=True).decode().strip()
    if confirmed != head + "\t" + remote_ref:
        raise AutoControllerError("Task branch push is unconfirmed; preserve run evidence")
    confirmation = {**intent, "remote_head": head}
    confirmation_path = plan.assignment_dir / "push-confirmed.json"
    if confirmation_path.exists() or confirmation_path.is_symlink():
        if _read_json(confirmation_path, maximum=8192) != confirmation:
            raise AutoControllerError("Saved push confirmation differs; preserve evidence")
    else:
        _save_new(confirmation_path, confirmation)

    review_tree = plan.assignment_dir / "source"
    if not review_tree.exists():
        _git(None, "clone", "--no-checkout", str(push_repo), str(review_tree), timeout=120)
        _git(review_tree, "switch", "-c", assignment["branch"], head)
        _git(review_tree, "remote", "set-url", "origin", remote)
    elif review_tree.is_symlink() or not review_tree.is_dir():
        raise AutoControllerError("Saved controller review source is unsafe")
    elif (_git(review_tree, "rev-parse", "HEAD").decode().strip() != head
          or _git(review_tree, "symbolic-ref", "--short", "HEAD").decode().strip()
          != assignment["branch"]
          or _git(review_tree, "remote", "get-url", "origin").decode().strip() != remote):
        raise AutoControllerError("Saved controller review source differs from confirmed push")
    if _git(review_tree, "status", "--porcelain", "--untracked-files=all"):
        raise AutoControllerError("Controller review source is not clean")
    workflow = _read_json(plan.assignment_dir / "assignment.json.workflow.json")
    token = workflow.get("token")
    message, key = result_message(assignment, result, relay_worker=plan.worker_id)
    bridge_intent = {"schema": "skybuild.cpu-result-intent.v1",
                     "project_id": plan.project_id, "task_id": prepared.task_id,
                     "assignment_id": prepared.assignment_id, "worker": plan.worker_id,
                     "attempt_id": token["attempt_id"], "claim_fence": token["claim_fence"],
                     "input_generation": token["input_generation"],
                     "definition_revision": token["definition_revision"],
                     "policy_version": token["policy_version"],
                     "assignment_sha256": hashlib.sha256(json.dumps(
                         assignment, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                     "brief_sha256": assignment["brief_sha256"], "patch_sha256": plan.patch_digest,
                     "source_head": head, "source_branch": remote_ref,
                     "target_base": assignment["base_sha"], "result": result,
                     "message": message, "message_idempotency_key": key}
    result_path = plan.assignment_dir / "result-intent.json"
    if result_path.exists() or result_path.is_symlink():
        if _read_json(result_path, maximum=65536) != bridge_intent:
            raise AutoControllerError("Saved result intent differs from exact worker output")
    else:
        _save_new(result_path, bridge_intent)
    return bridge_intent


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
                                                    "patch_sha256", "token_file"}
                or not isinstance(item["worker"], str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", item["worker"])
                or any(not isinstance(item[name], str) for name in item)):
            raise AutoControllerError("Candidate entry is invalid")
        if any(not Path(item[name]).is_absolute() or "\n" in item[name]
               for name in ("patch", "token_file")):
            raise AutoControllerError("Candidate token and patch paths must be absolute")
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
        pinned = {**envelope, "schema": "manual-work-v2", "task_status": task["status"],
                  "task_revision": task.get("revision")}
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
                        "priority": priority, "revision": task["revision"], "base_sha": envelope["base_sha"],
                        "brief_sha256": envelope["brief_sha256"], "envelope": pinned})
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
            if selected[0]["base_sha"] != selected[1]["base_sha"]:
                raise AutoControllerError("Selected tasks use different published bases")
            return selected
    raise AutoControllerError("Fewer than two distinct approved Ready assignments")


def _save_new(path: Path, value: dict) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _cpu_controls(client: Client, project: str, *, required_free: int = 2) -> None:
    identity = client.whoami()
    if not isinstance(identity, dict) or identity.get("is_admin") is not True:
        raise AutoControllerError("CPU control inspection requires owner authority")
    controls = client.cpu_control_status(project)
    pool = controls.get("pool") if isinstance(controls, dict) else None
    held = controls.get("held_units") if isinstance(controls, dict) else None
    if (type(required_free) is not int or required_free < 1
            or not isinstance(pool, dict) or pool.get("enabled") is not True
            or pool.get("local_enabled") is not True or type(pool.get("capacity")) is not int
            or type(held) is not int or pool["capacity"] - held < required_free):
        raise AutoControllerError("Owner CPU controls do not admit the remaining bounded workers")


def _observe_owned(manager: JobUnitManager, owned: list[dict], deadline: datetime) -> list[dict]:
    """Persist every known unit's latest state, including partial launch failures."""
    while True:
        complete = True
        for record in owned:
            try:
                state = manager.observe(record["unit"])
                record.update(phase=state.phase, result=state.result,
                              exit_status=state.exit_status, memory_peak_bytes=state.memory_peak_bytes)
            except JobUnitError:
                record.update(phase="unknown", result=None, exit_status=None)
            if record["phase"] != "completed":
                complete = False
        if complete or datetime.now(timezone.utc) >= deadline:
            return owned
        time.sleep(5)


def _private_json(path: Path, *, limit: int = 131072) -> dict:
    try:
        raw = _file_bytes(path, limit=limit, private=True)
        value = json.loads(raw)
    except (CPUWorkerBridgeError, ValueError, UnicodeError):
        raise AutoControllerError("Private recovery journal is malformed; preserve current exposure") from None
    if not isinstance(value, dict):
        raise AutoControllerError("Private recovery journal is not an object")
    return value


def _resume_run(*, repo: Path, manifest: Path, project: str, dispatcher: str, url: str,
                owner_token: Path, ca_file: Path, base_ref: str, state_dir: Path,
                permit_path: Path, permit_sha256: str, weekly_usage: Path,
                hostwatch: Path, controller_profile: Path) -> dict:
    """Recover only persisted dispatches. This path contains no start call."""
    selection = _private_json(state_dir / "selection.json")
    delivered = _private_json(state_dir / "delivered.json")
    if (selection.get("schema") != "skybuild.auto-patch-selection.v1"
            or selection.get("project_id") != project or selection.get("base_ref") != base_ref
            or selection.get("permit_sha256") != permit_sha256
            or selection.get("manifest_sha256") != hashlib.sha256(
                _file_bytes(manifest, limit=16384, private=False, owner=False)).hexdigest()
            or selection.get("approved_until") is None
            or delivered.get("messages") is None):
        raise AutoControllerError("Recovery inputs differ from the original approved run")
    selected = selection.get("selected")
    messages = delivered.get("messages")
    if not isinstance(selected, list) or len(selected) != 2 or not isinstance(messages, list) or len(messages) != 2:
        raise AutoControllerError("Recovery journal does not contain exactly two selected assignments")
    candidate_entries = _read_manifest(manifest)
    candidates = {entry["worker"]: entry for entry in candidate_entries}
    message_by_worker = {entry.get("worker"): entry for entry in messages if isinstance(entry, dict)}
    if len(message_by_worker) != 2:
        raise AutoControllerError("Delivered message journal is ambiguous")
    results = []
    permit = _private_json(permit_path, limit=16384)
    if hashlib.sha256(_file_bytes(permit_path, limit=16384, private=True)).hexdigest() != permit_sha256:
        raise AutoControllerError("Recovery permit differs from original approved bytes")
    ca_digest = ca_file_sha256(ca_file)
    for saved in selected:
        worker = saved.get("worker") if isinstance(saved, dict) else None
        item = candidates.get(worker)
        delivery = message_by_worker.get(worker)
        if not isinstance(item, dict) or not isinstance(delivery, dict):
            raise AutoControllerError("Recovery manifest omits an original worker")
        envelope = build_envelope(repo, item["brief_path"], worker=worker,
                                  dispatcher=dispatcher, base_ref=base_ref)
        if (envelope["task_id"] != saved.get("task_id")
                or envelope["assignment_id"] != saved.get("assignment_id")
                or envelope["branch"] != saved.get("branch")
                or envelope["base_sha"] != saved.get("base_sha")
                or envelope["brief_sha256"] != saved.get("brief_sha256")
                or item["patch_sha256"] != saved.get("patch_sha256")
                or delivery.get("task_id") != saved.get("task_id")
                or delivery.get("message_id") is None):
            raise AutoControllerError("Recovery assignment differs from the original dispatch")
        _patch_bytes(Path(item["patch"]), item["patch_sha256"])
        worker_root = state_dir / ("worker-" + worker)
        runtime_state = worker_root / "runtime"
        assignment_dir = runtime_state / "assignment"
        dispatch_path = worker_root / "dispatch-intent.json"
        launch_path = worker_root / "launch-intent.json"
        dispatch_intent = _private_json(dispatch_path, limit=16384)
        if not launch_path.is_file() or launch_path.is_symlink():
            results.append({"worker": worker, "task_id": saved["task_id"], "submitted": False,
                            "settled": False,
                            "reason": "No durable pre-start identity exists; retain exposure for owner reconciliation"})
            continue
        journal = _private_json(launch_path, limit=16384)
        if (dispatch_intent.get("schema") != "skybuild.auto-patch-cpu-dispatch-intent.v1"
                or dispatch_intent.get("task_id") != saved["task_id"]
                or dispatch_intent.get("worker") != worker
                or dispatch_intent.get("assignment_id") != saved["assignment_id"]
                or dispatch_intent.get("permit_sha256") != permit_sha256
                or dispatch_intent.get("action_id") != journal.get("action_id")
                or dispatch_intent.get("operation_id") != journal.get("operation_id")
                or dispatch_intent.get("attempt_id") != journal.get("attempt_id")
                or dispatch_intent.get("claim_fence") != journal.get("claim_fence")):
            raise AutoControllerError("Recovery dispatch and launch journals disagree")
        reservation = dispatch_intent.get("reservation")
        if (not isinstance(reservation, dict) or reservation.get("task_id") != saved["task_id"]
                or reservation.get("action_id") != journal.get("action_id")
                or reservation.get("attempt_id") != journal.get("attempt_id")
                or reservation.get("claim_fence") != journal.get("claim_fence")):
            raise AutoControllerError("Recovery reservation does not bind the original claim")
        plan = CPUWorkerPlan(
            project_id=project, worker_id=worker, dispatcher_id=dispatcher, url=url,
            checkout=repo, assignment_dir=assignment_dir, patch_file=Path(item["patch"]),
            patch_digest=item["patch_sha256"], worker_token_file=Path(item["token_file"]),
            worker_image_id=permit["worker_image_id"],
            worker_input_dir=worker_root / "input", worker_output_dir=worker_root / "output",
            worker_source_dir=worker_root / "source-mount", ca_file=ca_file,
            permit_file=permit_path, permit_digest=permit_sha256, owner_token_file=owner_token,
            weekly_usage_file=weekly_usage, hostwatch_file=hostwatch,
            external_state_dir=runtime_state, controller_profile_file=controller_profile)
        try:
            prepared = recover_worker(plan, journal)
            manager = JobUnitManager(runtime_state)
            unit_state = manager.observe(prepared.unit_name)
            if (unit_state.phase == "completed" and unit_state.result == "success"
                    and unit_state.exit_status == 0):
                container = _docker_container_state(prepared)
                if container.get("status") == "exited" and container.get("exit_code") == 0:
                    _publish_worker_output(prepared, journal)
            outcome = reconcile_worker(prepared)
            results.append({"worker": worker, "task_id": saved["task_id"],
                            "operation_id": prepared.operation_id,
                            "unit": prepared.unit_name, **outcome})
        except (AutoControllerError, ClientError, CPUWorkerBridgeError, JobUnitError,
                OSError, ValueError, TypeError) as error:
            results.append({"worker": worker, "task_id": saved["task_id"],
                            "submitted": False, "settled": False,
                            "reconciliation_error": type(error).__name__})
    output = {"schema": "skybuild.auto-patch-run.v1", "resumed": True,
              "selected": len(selected), "workers": results,
              "state_dir": str(state_dir),
              "submitted": len(results) == 2 and all(item.get("submitted") is True for item in results)}
    _save_new(state_dir / ("recovery-" + uuid4().hex + ".json"), output)
    return output


def run(*, repo: Path, manifest: Path, project: str, dispatcher: str, url: str,
        dispatcher_token: Path, owner_token: Path, ca_file: Path, base_ref: str,
        state_dir: Path, permit_path: Path, permit_sha256: str, weekly_usage: Path,
        hostwatch: Path, controller_profile: Path, resume: bool = False) -> dict:
    _private_endpoint(url, _resolved_addresses)
    repo = repo.resolve()
    state_dir = _state_directory(state_dir, repo)
    existing = list(state_dir.iterdir())
    if existing:
        if not resume:
            raise AutoControllerError("Run directory exists; use explicit reconcile-only --resume")
        return _resume_run(repo=repo, manifest=manifest, project=project, dispatcher=dispatcher,
                           url=url, owner_token=owner_token, ca_file=ca_file, base_ref=base_ref,
                           state_dir=state_dir, permit_path=permit_path,
                           permit_sha256=permit_sha256, weekly_usage=weekly_usage,
                           hostwatch=hostwatch, controller_profile=controller_profile)
    if resume:
        raise AutoControllerError("No existing worker journal is available to resume")
    candidates = _read_manifest(manifest)
    manifest_digest = hashlib.sha256(
        _file_bytes(manifest, limit=16384, private=False, owner=False)).hexdigest()
    ca_digest = ca_file_sha256(ca_file)
    with Client(url, _token_from_file(dispatcher_token), retries=0, timeout=10,
                trust_env=False, ca_file=ca_file, expected_ca_sha256=ca_digest) as client:
        selected = select(client, repo, candidates, project=project,
                          dispatcher=dispatcher, base_ref=base_ref)
    permit = load_permit(permit_path, permit_sha256, checkout=repo, selected=selected,
                         project=project, base_ref=base_ref, hostwatch=hostwatch,
                         usage=weekly_usage)
    expiry = datetime.fromisoformat(permit["approved_until"].replace("Z", "+00:00"))
    with Client(url, _token_from_file(owner_token), retries=0, timeout=10,
                trust_env=False, ca_file=ca_file, expected_ca_sha256=ca_digest) as owner:
        _cpu_controls(owner, project)
    _save_new(state_dir / "selection.json", {"schema": "skybuild.auto-patch-selection.v1",
              "project_id": project, "base_ref": base_ref, "permit_sha256": permit_sha256,
              "manifest_sha256": manifest_digest,
              "approved_until": permit["approved_until"],
              "selected": [{key: item[key] for key in ("task_id", "assignment_id", "worker", "branch",
                                                     "priority", "revision", "base_sha", "brief_sha256",
                                                     "patch_sha256")}
                           for item in selected]})
    delivered = []
    for item in selected:
        if datetime.now(timezone.utc) >= expiry:
            raise AutoControllerError("Approval expired before dispatch")
        check_source(repo, permit)
        check_weekly_usage(weekly_usage, permit)
        resource_admission(hostwatch, permit, selected_count=2)
        response = dispatch(repo, item["brief_path"], worker=item["worker"], dispatcher=dispatcher,
                            project=project, principal=dispatcher, url=url, token_file=dispatcher_token,
                            state_dir=state_dir / "dispatch", ca_file=ca_file, base_ref=base_ref,
                            expected_envelope=item["envelope"])
        delivered.append({**item, "message_id": response["message_id"]})
    _save_new(state_dir / "delivered.json", {"messages": [
        {"task_id": item["task_id"], "worker": item["worker"], "message_id": item["message_id"]}
        for item in delivered]})
    owned = []
    prepared_runs = {}
    failure = None
    for item in delivered:
        try:
            if datetime.now(timezone.utc) >= expiry:
                raise AutoControllerError("Approval expired before worker claim")
            check_source(repo, permit)
            check_weekly_usage(weekly_usage, permit)
            resource_admission(hostwatch, permit, selected_count=2)
            with Client(url, _token_from_file(owner_token), retries=0, timeout=10,
                        trust_env=False, ca_file=ca_file, expected_ca_sha256=ca_digest) as owner:
                _cpu_controls(owner, project, required_free=1)
            probe_private_api(url, project, Path(item["token_file"]), item["worker"],
                              ca_file=ca_file, workflow=True)
            worker_root = state_dir / ("worker-" + item["worker"])
            worker_root.mkdir(mode=0o700, exist_ok=False)
            runtime_state = worker_root / "runtime"
            runtime_state.mkdir(mode=0o700, exist_ok=False)
            assignment_dir = runtime_state / "assignment"
            assignment_dir.mkdir(mode=0o700, exist_ok=False)
            with Client(url, _token_from_file(Path(item["token_file"])), retries=0, timeout=10,
                        trust_env=False, ca_file=ca_file, expected_ca_sha256=ca_digest) as worker_client:
                current = worker_client.get_task(project, item["task_id"])
                _approved_task(current, item["envelope"], item["patch_sha256"])
                received = receive_assignment(worker_client, project, repo, worker=item["worker"],
                                              dispatcher=dispatcher, message_id=item["message_id"],
                                              destination=assignment_dir / "assignment.json",
                                              expected_envelope=item["envelope"])
            if received.get("place") != "working" or not received.get("attempt_id"):
                raise AutoControllerError("Worker claim has no fenced attempt")
            _save_new(assignment_dir / "preclaim.json", received)
            current = None
            with Client(url, _token_from_file(Path(item["token_file"])), retries=0, timeout=10,
                        trust_env=False, ca_file=ca_file, expected_ca_sha256=ca_digest) as worker_client:
                current = worker_client.get_task(project, item["task_id"])
                # CPU-control inspection is owner-only. Keep the worker token for
                # the fenced reservation below, but read the current pool snapshot
                # with the already-authorized owner principal after claim.
                with Client(url, _token_from_file(owner_token), retries=0, timeout=10,
                            trust_env=False, ca_file=ca_file, expected_ca_sha256=ca_digest) as owner_client:
                    status = owner_client.cpu_control_status(project)
                readiness = current.get("metadata", {}).get("_skybuild_workflow", {}).get("readiness", {})
                pool = status.get("pool") if isinstance(status, dict) else None
                if (not isinstance(readiness, dict) or type(readiness.get("input_generation")) is not int
                        or not isinstance(pool, dict) or pool.get("enabled") is not True
                        or pool.get("local_enabled") is not True):
                    raise AutoControllerError("Current CPU readiness or controls are unavailable")
                action_id, operation_id = uuid4().hex, uuid4().hex
                reservation_request = {
                    "task_id": item["task_id"], "action_id": action_id,
                    "attempt_id": received["attempt_id"], "units": 1,
                    "expected_revision": current["revision"],
                    "readiness_generation": readiness["input_generation"],
                    "claim_fence": received["claim_fence"],
                    "generation": pool["generation"],
                    "local_generation": pool["local_generation"],
                }
                _save_new(worker_root / "dispatch-intent.json", {
                    "schema": "skybuild.auto-patch-cpu-dispatch-intent.v1",
                    "task_id": item["task_id"], "worker": item["worker"],
                    "assignment_id": item["assignment_id"], "attempt_id": received["attempt_id"],
                    "claim_fence": received["claim_fence"], "action_id": action_id,
                    "operation_id": operation_id, "reservation": reservation_request,
                    "permit_sha256": permit_sha256,
                })
                reservation = worker_client.reserve_cpu(project, reservation_request)
                if (reservation.get("action_id") != action_id or reservation.get("state") != "reserved"
                        or reservation.get("attempt_id") != received["attempt_id"]):
                    raise AutoControllerError("CPU reservation does not bind exact worker attempt")
            worker_input_dir = worker_root / "input"
            worker_output_dir = worker_root / "output"
            worker_source_dir = _prepare_worker_source(
                repo, worker_root / "source-mount", item["base_sha"])
            worker_input_dir.mkdir(mode=0o700)
            worker_output_dir.mkdir(mode=0o700)
            for name, source in (("assignment.json", assignment_dir / "assignment.json"),
                                 ("preclaim.json", assignment_dir / "preclaim.json")):
                _save_new(worker_input_dir / name, _read_json(source))
            patch_input = worker_input_dir / "approved.patch"
            _write_exclusive(patch_input, _file_bytes(Path(item["patch"]), limit=65536, private=True),
                             mode=0o400)
            worker_input_dir.chmod(0o500)
            plan = CPUWorkerPlan(
                project_id=project, worker_id=item["worker"], dispatcher_id=dispatcher,
                url=url, checkout=repo, assignment_dir=assignment_dir,
                patch_file=Path(item["patch"]), patch_digest=item["patch_sha256"],
                worker_token_file=Path(item["token_file"]), worker_image_id=permit["worker_image_id"],
                worker_input_dir=worker_input_dir, worker_output_dir=worker_output_dir,
                worker_source_dir=worker_source_dir,
                ca_file=ca_file, permit_file=permit_path, permit_digest=permit_sha256,
                owner_token_file=owner_token, weekly_usage_file=weekly_usage,
                hostwatch_file=hostwatch, external_state_dir=runtime_state,
                controller_profile_file=controller_profile,
            )
            prepared = prepare_worker(plan, action_id=action_id, operation_id=operation_id)
            prepared_runs[operation_id] = prepared
            if datetime.now(timezone.utc) >= expiry:
                raise AutoControllerError("Approval expired before bounded unit launch")
            check_source(repo, permit)
            check_weekly_usage(weekly_usage, permit)
            resource_admission(hostwatch, permit, selected_count=2)
            record = {"task_id": item["task_id"], "worker": item["worker"],
                      "schema": "skybuild.cpu-worker-launch.v1",
                      "assignment_id": item["assignment_id"], "attempt_id": received["attempt_id"],
                      "claim_fence": received["claim_fence"], "action_id": action_id,
                      "operation_id": operation_id, "unit": prepared.unit_name,
                      "launch_nonce": prepared.launch_nonce,
                      "approved_until": prepared.approved_until.isoformat(),
                      "source_head": prepared.source_head,
                      "source_digest": prepared.source_digest,
                      "interpreter_digest": prepared.interpreter_digest,
                      "controller_head": prepared.controller_head,
                      "controller_source_digest": prepared.controller_source_digest,
                      "controller_profile_digest": prepared.controller_profile_digest,
                      "ca_digest": prepared.ca_digest,
                      "owner_token_digest": prepared.owner_token_digest,
                      "worker_token_digest": prepared.worker_token_digest,
                      "container_name": prepared.container_name,
                      "container_run_id": prepared.container_run_id,
                      "log": str(prepared.spec.log_path),
                      "assignment_digest": prepared.assignment_digest,
                      "argv_digest": prepared.argv_digest,
                      "phase": "launch_intent", "assignment_dir": str(assignment_dir)}
            _save_new(worker_root / "launch-intent.json", record)
            owned.append(record)
            launched = launch_worker(prepared)
            if launched.get("started") is not True or launched.get("unit_name") != prepared.unit_name:
                raise AutoControllerError("Trusted bridge did not confirm the one-shot unit start")
        except (AutoControllerError, DispatchError, ManualCordError, ClientError, PreflightError,
                PermitError, JobUnitError, CPUWorkerBridgeError, OSError, ValueError, TypeError) as error:
            failure = type(error).__name__
            break
    deadline = min(expiry, datetime.now(timezone.utc) + timedelta(seconds=permit["runtime_seconds"] + 30))
    results = []
    for record in owned:
        submitted_path = Path(record["assignment_dir"]) / "submitted.json"
        if record.get("phase") != "launch_intent":
            record["submitted"] = False
            results.append(record)
            continue
        try:
            manager = JobUnitManager(Path(record["assignment_dir"]).parent)
            while datetime.now(timezone.utc) < deadline:
                unit_state = manager.observe(record["unit"])
                record.update(phase=unit_state.phase, result=unit_state.result,
                              exit_status=unit_state.exit_status,
                              memory_peak_bytes=unit_state.memory_peak_bytes)
                if unit_state.phase == "completed":
                    break
                time.sleep(5)
            if record.get("phase") != "completed":
                record.update(phase="unknown", result=None, exit_status=None)
            prepared = prepared_runs.get(record["operation_id"])
            if prepared is None:
                raise AutoControllerError("In-process worker handle is missing; use reconcile-only resume")
            if (record.get("phase") == "completed" and record.get("result") == "success"
                    and record.get("exit_status") == 0):
                container = _docker_container_state(prepared)
                if container.get("status") == "exited" and container.get("exit_code") == 0:
                    _publish_worker_output(prepared, record)
            outcome = reconcile_worker(prepared)
            record.update(settled=outcome.get("settled") is True,
                          submitted=outcome.get("submitted") is True,
                          reconciliation_reason=outcome.get("reason"))
        except (AutoControllerError, ClientError, CPUWorkerBridgeError, JobUnitError,
                OSError, ValueError, TypeError) as error:
            record.update(submitted=False, reconciliation_error=type(error).__name__)
        results.append(record)
    output = {"schema": "skybuild.auto-patch-run.v1", "selected": len(selected),
              "workers": results, "state_dir": str(state_dir),
              "submitted": failure is None and len(results) == 2 and all(r["submitted"] for r in results),
              "failure": failure}
    _save_new(state_dir / "outcome.json", output)
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("url", "project", "dispatcher", "base-ref", "permit-sha256"):
        parser.add_argument("--" + name, required=True)
    for name in ("checkout", "manifest", "dispatcher-token", "owner-token", "ca-file", "state-dir",
                 "permit", "weekly-usage", "hostwatch", "controller-profile"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--resume", action="store_true",
                        help="reconcile existing durable worker journals; never dispatch or start")
    args = parser.parse_args(argv)
    try:
        result = run(repo=args.checkout, manifest=args.manifest, project=args.project,
                     dispatcher=args.dispatcher, url=args.url, dispatcher_token=args.dispatcher_token,
                     owner_token=args.owner_token, ca_file=args.ca_file, base_ref=args.base_ref,
                     state_dir=args.state_dir, permit_path=args.permit, permit_sha256=args.permit_sha256,
                     weekly_usage=args.weekly_usage, hostwatch=args.hostwatch,
                     controller_profile=args.controller_profile, resume=args.resume)
        print(json.dumps(result, sort_keys=True))
        return 0 if result["submitted"] else 2
    except (AutoControllerError, DispatchError, ManualCordError, ClientError, PreflightError,
            PermitError, JobUnitError, CPUWorkerBridgeError, OSError, ValueError, TypeError, subprocess.SubprocessError):
        print(json.dumps({"submitted": False, "reason": "Controller stopped; preserve private run evidence"}),
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    # The trusted bridge checks the controller module origin. When invoked as
    # ``python -m``, expose this exact file under its package name as well.
    sys.modules.setdefault("skybuild.auto_patch_controller", sys.modules[__name__])
    raise SystemExit(main())
