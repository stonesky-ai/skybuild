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


def load(path: Path, expected_sha256: str, *, checkout: Path, selected: list[dict],
         project: str, base_ref: str, hostwatch: Path, usage: Path) -> dict:
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise PermitError("Approved permit digest is invalid")
    raw = _private_bytes(path, 16384)
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise PermitError("Approved permit bytes changed")
    permit = json.loads(raw)
    fields = {"schema", "project_id", "profile", "source_head", "base_ref", "host_id", "slots",
              "approved_until", "weekly_usage_sha256", "hostwatch_reserve_bytes",
              "memory_high_bytes", "memory_max_bytes", "runtime_seconds", "workers"}
    if not isinstance(permit, dict) or set(permit) != fields:
        raise PermitError("Approved permit contract is invalid")
    if (permit["schema"] != "skybuild.auto-cpu-patch-permit.v1"
            or permit["profile"] != "bounded-trusted-cpu-patch-v1"
            or permit["project_id"] != project or permit["base_ref"] != base_ref
            or permit["host_id"] != socket.gethostname()
            or permit["slots"] != 2):
        raise PermitError("Permit scope differs from this two-worker route")
    source = subprocess.run(["git", "rev-parse", "HEAD"], cwd=checkout,
                            capture_output=True, check=False, timeout=5)
    if (source.returncode or not re.fullmatch(r"[0-9a-f]{40}", permit["source_head"])
            or source.stdout.decode().strip() != permit["source_head"]):
        raise PermitError("Controller source differs from approved exact head")
    expected_workers = [{key: item[key] for key in ("task_id", "worker", "assignment_id",
                                                  "brief_path", "branch", "patch_sha256")}
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
    usage_raw = _private_bytes(usage, 16384)
    if hashlib.sha256(usage_raw).hexdigest() != permit["weekly_usage_sha256"]:
        raise PermitError("Weekly usage observation differs from approved bytes")
    observation = json.loads(usage_raw)
    now = datetime.now(timezone.utc)
    if (not isinstance(observation, dict) or observation.get("production_must_drain") is not False
            or observation.get("stop_production_percent") != 50
            or type(observation.get("weekly_used_percent")) not in (int, float)
            or not 0 <= observation["weekly_used_percent"] < 50
            or _when(observation.get("confirmed_at")) > now
            or _when(observation.get("valid_until")) <= now
            or _when(permit["approved_until"]) > _when(observation["valid_until"])):
        raise PermitError("Weekly usage is stale or at the stop threshold")
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
