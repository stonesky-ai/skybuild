"""One-shot, permit-bound delivery of two approved deterministic CPU patches.

The controller selects Ready tasks itself, durably dispatches and claims each
assignment, and launches distinct bounded systemd attempt units. A used run
directory is never replayed; uncertain effects require operator reconciliation.
"""

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from uuid import uuid4

from scripts.skybuild_job_unit import JobUnitError, JobUnitManager

from .auto_patch_permit import (PermitError, check_source, check_weekly_usage,
                                load as load_permit, resource_admission)
from .auto_patch_worker import _approved_task, _patch_bytes
from .client import Client, ClientError, ca_file_sha256
from .fleet_preflight import PreflightError, _token_from_file, _resolved_addresses, probe_private_api
from .manual_cord import ManualCordError, receive_assignment
from .cpu_worker_bridge import (CPUWorkerBridgeError, CPUWorkerPlan, launch_worker,
                                prepare_worker, reconcile_worker)
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
                or any(not isinstance(item[name], str) for name in item)):
            raise AutoControllerError("Candidate entry is invalid")
        if any(not Path(item[name]).is_absolute() or "\n" in item[name]
               for name in ("patch", "token_file", "git_token_file")):
            raise AutoControllerError("Candidate credential and patch paths must be absolute")
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


def run(*, repo: Path, manifest: Path, project: str, dispatcher: str, url: str,
        dispatcher_token: Path, owner_token: Path, ca_file: Path, base_ref: str,
        state_dir: Path, permit_path: Path, permit_sha256: str, weekly_usage: Path,
        hostwatch: Path, controller_profile: Path) -> dict:
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
    permit = load_permit(permit_path, permit_sha256, checkout=repo, selected=selected,
                         project=project, base_ref=base_ref, hostwatch=hostwatch,
                         usage=weekly_usage)
    expiry = datetime.fromisoformat(permit["approved_until"].replace("Z", "+00:00"))
    with Client(url, _token_from_file(owner_token), retries=0, timeout=10,
                trust_env=False, ca_file=ca_file, expected_ca_sha256=ca_digest) as owner:
        _cpu_controls(owner, project)
    _save_new(state_dir / "selection.json", {"schema": "skybuild.auto-patch-selection.v1",
              "project_id": project, "base_ref": base_ref, "permit_sha256": permit_sha256,
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
                status = worker_client.cpu_control_status(project)
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
            plan = CPUWorkerPlan(
                project_id=project, worker_id=item["worker"], dispatcher_id=dispatcher,
                url=url, checkout=repo, assignment_dir=assignment_dir,
                patch_file=Path(item["patch"]), patch_digest=item["patch_sha256"],
                worker_token_file=Path(item["token_file"]), git_token_file=Path(item["git_token_file"]),
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
                      "assignment_id": item["assignment_id"], "attempt_id": received["attempt_id"],
                      "claim_fence": received["claim_fence"], "action_id": action_id,
                      "operation_id": operation_id, "unit": prepared.unit_name,
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
            from scripts.skybuild_job_unit import JobUnitManager
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
                raise AutoControllerError("Process restart requires explicit durable bridge reconstruction")
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
    args = parser.parse_args(argv)
    try:
        result = run(repo=args.checkout, manifest=args.manifest, project=args.project,
                     dispatcher=args.dispatcher, url=args.url, dispatcher_token=args.dispatcher_token,
                     owner_token=args.owner_token, ca_file=args.ca_file, base_ref=args.base_ref,
                     state_dir=args.state_dir, permit_path=args.permit, permit_sha256=args.permit_sha256,
                     weekly_usage=args.weekly_usage, hostwatch=args.hostwatch,
                     controller_profile=args.controller_profile)
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
