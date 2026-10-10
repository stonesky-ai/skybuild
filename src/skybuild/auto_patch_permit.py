"""Verify one externally approved, hash-pinned two-worker CPU patch window."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys


class PermitError(ValueError):
    pass


def _private_bytes(path: Path, limit: int) -> bytes:
    if not path.is_absolute() or path.is_symlink():
        raise PermitError("Evidence path must be absolute and not a symlink")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= limit:
            raise PermitError("Evidence file is missing or exceeds its bound")
        return os.read(descriptor, limit + 1)
    finally:
        os.close(descriptor)


def _when(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError, OverflowError):
        raise PermitError("Evidence time is invalid") from None
    if parsed.tzinfo is None:
        raise PermitError("Evidence time needs an offset")
    return parsed.astimezone(timezone.utc)


def envelope_sha256(envelope: dict) -> str:
    payload = json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def check_source(checkout: Path, permit: dict, *, require_job_unit: bool = True) -> None:
    """Bind the executing checkout, loaded modules and clean source bytes."""
    if any(name.startswith("GIT_") and name != "GIT_PAGER" for name in os.environ):
        raise PermitError("Inherited Git configuration can hide source changes")
    source = subprocess.run(["git", "rev-parse", "HEAD"], cwd=checkout,
                            capture_output=True, check=False, timeout=5)
    if (source.returncode or not re.fullmatch(r"[0-9a-f]{40}", permit["source_head"])
            or source.stdout.decode().strip() != permit["source_head"]):
        raise PermitError("Controller source differs from approved exact head")
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=checkout,
                         capture_output=True, check=False, timeout=5)
    status = subprocess.run(["git", "status", "--porcelain=v1", "--untracked-files=all", "-z"],
                            cwd=checkout, capture_output=True, check=False, timeout=10)
    tracked = subprocess.run(["git", "ls-files", "-z"], cwd=checkout,
                             capture_output=True, check=False, timeout=10)
    if (top.returncode or Path(top.stdout.decode().strip()).resolve() != checkout.resolve()
            or status.returncode or status.stdout or tracked.returncode):
        raise PermitError("Executing checkout is dirty or not the approved root")
    tracked_paths = set(tracked.stdout.split(b"\0"))
    source_root = checkout / "src" / "skybuild"
    loaded = {name: module for name, module in sys.modules.items()
              if (name == "skybuild" or name.startswith("skybuild.")) and getattr(module, "__file__", None)}
    if not loaded or any(not Path(module.__file__).resolve().is_relative_to(source_root)
                         for module in loaded.values()):
        raise PermitError("Loaded SkyBuild module is outside the approved checkout")
    job_unit = sys.modules.get("scripts.skybuild_job_unit")
    if require_job_unit and (job_unit is None or not getattr(job_unit, "__file__", None)
                             or Path(job_unit.__file__).resolve() != checkout / "scripts" / "skybuild_job_unit.py"):
        raise PermitError("Loaded job unit is outside the approved checkout")
    for module in (*loaded.values(), *((job_unit,) if require_job_unit else ())):
        path = Path(module.__file__)
        relative = path.resolve().relative_to(checkout).as_posix()
        if (path.is_symlink() or not path.is_file() or path.stat().st_size > 2_097_152
                or os.fsencode(relative) not in tracked_paths):
            raise PermitError("Loaded source is not a bounded tracked regular file")
        committed = subprocess.run(["git", "show", "HEAD:" + relative], cwd=checkout,
                                   capture_output=True, check=False, timeout=5)
        if committed.returncode or committed.stdout != path.read_bytes():
            raise PermitError("Loaded source bytes differ from approved head")
    main = sys.modules.get("__main__")
    entry = getattr(getattr(main, "__spec__", None), "name", None)
    if (entry in {"skybuild.auto_patch_controller", "skybuild.auto_patch_worker"}
            and Path(main.__file__).resolve() != checkout / "src" / "skybuild" / (entry.rsplit(".", 1)[1] + ".py")):
        raise PermitError("Executing worker or controller is outside the approved checkout")
    if entry in {"skybuild.auto_patch_controller", "skybuild.auto_patch_worker"}:
        path = Path(main.__file__)
        relative = path.resolve().relative_to(checkout).as_posix()
        committed = subprocess.run(["git", "show", "HEAD:" + relative], cwd=checkout,
                                   capture_output=True, check=False, timeout=5)
        if (path.is_symlink() or not path.is_file() or path.stat().st_size > 2_097_152
                or os.fsencode(relative) not in tracked_paths or committed.returncode
                or committed.stdout != path.read_bytes()):
            raise PermitError("Executing worker or controller bytes differ from approved head")


def check_worker_permit(path: Path, expected_sha256: str, *, checkout: Path,
                        assignment: dict, project: str, worker: str, patch_sha256: str,
                        approved_until: datetime) -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise PermitError("Worker permit digest is invalid")
    raw = _private_bytes(path, 16384)
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise PermitError("Worker permit bytes differ from approved controller input")
    permit = json.loads(raw)
    if (not isinstance(permit, dict) or permit.get("schema") != "skybuild.auto-cpu-patch-permit.v1"
            or permit.get("profile") != "bounded-trusted-cpu-patch-v1"
            or permit.get("project_id") != project or permit.get("host_id") != socket.gethostname()
            or permit.get("slots") != 2 or _when(permit.get("approved_until")) != approved_until
            or approved_until <= datetime.now(timezone.utc)):
        raise PermitError("Worker permit scope or interval differs")
    selected = permit.get("workers")
    expected = {"task_id": assignment["task_id"], "worker": worker,
                "assignment_id": assignment["assignment_id"], "brief_path": assignment["brief_path"],
                "brief_sha256": assignment["brief_sha256"], "branch": assignment["branch"],
                "base_sha": assignment["base_sha"], "revision": assignment["task_revision"],
                "patch_sha256": patch_sha256, "envelope_sha256": envelope_sha256(assignment)}
    if (not isinstance(selected, list) or len(selected) != 2
            or sum(item == expected for item in selected) != 1):
        raise PermitError("Worker assignment differs from approved exact permit")
    check_source(checkout, permit, require_job_unit=False)


def load(path: Path, expected_sha256: str, *, checkout: Path, selected: list[dict],
         project: str, base_ref: str, hostwatch: Path, usage: Path) -> dict:
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise PermitError("Approved permit digest is invalid")
    raw = _private_bytes(path, 16384)
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise PermitError("Approved permit bytes changed")
    permit = json.loads(raw)
    fields = {"schema", "project_id", "profile", "source_head", "base_ref", "host_id", "slots",
              "approved_until", "hostwatch_reserve_bytes",
              "memory_high_bytes", "memory_max_bytes", "runtime_seconds", "worker_image_id", "workers"}
    if (not isinstance(permit, dict)
            or set(permit) not in (fields | {"usage"}, fields | {"weekly_usage_sha256"})):
        raise PermitError("Approved permit contract is invalid")
    if (permit["schema"] != "skybuild.auto-cpu-patch-permit.v1"
            or permit["profile"] != "bounded-trusted-cpu-patch-v1"
            or permit["project_id"] != project or permit["base_ref"] != base_ref
            or permit["host_id"] != socket.gethostname()
            or permit["slots"] != 2):
        raise PermitError("Permit scope differs from this two-worker route")
    if not isinstance(permit["worker_image_id"], str) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", permit["worker_image_id"]):
        raise PermitError("Permit needs one immutable CPU worker image ID")
    check_source(checkout, permit)
    expected_workers = [{**{key: item[key] for key in ("task_id", "worker", "assignment_id",
                                                         "brief_path", "brief_sha256", "branch",
                                                         "base_sha", "revision", "patch_sha256")},
                         "envelope_sha256": envelope_sha256(item["envelope"])}
                        for item in selected]
    if permit["workers"] != expected_workers:
        raise PermitError("Selected task and patch identities differ from permit")
    for name in ("hostwatch_reserve_bytes", "memory_high_bytes", "memory_max_bytes",
                 "runtime_seconds"):
        if type(permit[name]) is not int or permit[name] <= 0:
            raise PermitError("Permit resource bound is invalid")
    if (permit["hostwatch_reserve_bytes"] < 8 * 1024**3
            or not permit["memory_high_bytes"] < permit["memory_max_bytes"] <= 4 * 1024**3
            or permit["runtime_seconds"] > 1800):
        raise PermitError("Permit exceeds bounded CPU worker resource policy")
    if _when(permit["approved_until"]) <= datetime.now(timezone.utc):
        raise PermitError("Approved work interval has expired")
    check_weekly_usage(usage, permit)
    resource_admission(hostwatch, permit, selected_count=2)
    return permit


def check_weekly_usage(usage: Path, permit: dict) -> dict:
    """Recheck the exact approved observation before each external effect."""
    pin = permit.get("usage")
    revised = False
    if pin is not None:
        if (not isinstance(pin, dict)
                or set(pin) not in ({"path", "sha256", "valid_until"},
                                    {"path", "sha256", "valid_until", "owner_policy"})
                or pin.get("path") != str(usage)):
            raise PermitError("Weekly usage pin is invalid or names another path")
        revised = "owner_policy" in pin
        if revised:
            owner_pin = pin["owner_policy"]
            if (not isinstance(owner_pin, dict) or set(owner_pin) != {"path", "sha256"}
                    or not isinstance(owner_pin["path"], str)):
                raise PermitError("Owner policy pin is invalid")
            owner_raw = _private_bytes(Path(owner_pin["path"]), 16384)
            owner = json.loads(owner_raw)
            if (hashlib.sha256(owner_raw).hexdigest() != owner_pin["sha256"]
                    or not isinstance(owner, dict)
                    or owner.get("schema") != "skybuild.usage-policy-owner-revision.v1"
                    or owner.get("production_allowed") is not True
                    or "weekly_production_stop_percent" not in owner
                    or owner["weekly_production_stop_percent"] is not None):
                raise PermitError("Pinned owner revision does not remove the weekly cutoff")
    usage_raw = _private_bytes(usage, 16384)
    expected = pin["sha256"] if pin is not None else permit.get("weekly_usage_sha256")
    if hashlib.sha256(usage_raw).hexdigest() != expected:
        raise PermitError("Weekly usage observation differs from approved bytes")
    observation = json.loads(usage_raw)
    now = datetime.now(timezone.utc)
    if (not isinstance(observation, dict)
            or type(observation.get("weekly_used_percent")) not in (int, float)
            or not 0 <= observation["weekly_used_percent"] <= 100
            or (pin is not None and observation.get("valid_until") != pin["valid_until"])
            or _when(observation.get("confirmed_at")) > now
            or _when(observation.get("valid_until")) <= now
            or _when(permit["approved_until"]) > _when(observation["valid_until"])):
        raise PermitError("Weekly usage is stale or invalid")
    if not revised and (observation.get("production_must_drain") is not False
                        or observation.get("stop_production_percent") != 50
                        or not observation["weekly_used_percent"] < 50):
        raise PermitError("Legacy weekly usage requires production to drain")
    return observation


def resource_admission(hostwatch: Path, permit: dict, *, selected_count: int) -> dict:
    """Require a fresh host observation before each bounded unit launch."""
    sample = json.loads(_private_bytes(hostwatch, 262144))
    now = datetime.now(timezone.utc)
    observed = _when(sample.get("sampled_at"))
    if (observed > now or (now - observed).total_seconds() > 120
            or sample.get("status") != "ok"
            or sample.get("reserve_bytes") != permit["hostwatch_reserve_bytes"]
            or type(sample.get("available_bytes")) is not int
            or sample["available_bytes"] < (permit["hostwatch_reserve_bytes"]
                                             + selected_count * permit["memory_max_bytes"])):
        raise PermitError("Fresh host resource admission is unavailable")
    capacity = sample.get("capacity")
    if capacity is not None and (not isinstance(capacity, dict)
                                 or capacity.get("status") != "ok"
                                 or type(capacity.get("max_new_jobs")) is not int
                                 or capacity["max_new_jobs"] < selected_count):
        raise PermitError("Host worker capacity does not admit selected jobs")
    return sample
