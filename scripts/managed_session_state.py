"""Inspect a managed service or release its slot after verified recovery.

Recovery does not stop a process, start a service, or publish code. An operator
must supply evidence that resolves publication for the exact service and ref.
"""

from __future__ import annotations

import argparse
import hashlib
import fcntl
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import time

from managed_session import (SessionError, checked, private_directory, read_record,
                             service_environment, write_new)

UNIT = re.compile(r"skybuild-(analysis|merge)-[0-9a-f]{32}\.service\Z")
INVOCATION = re.compile(r"[0-9a-f]{32}\Z")


def launcher_alive(record):
    identity = record.get("launcher")
    if (not isinstance(identity, dict) or type(identity.get("pid")) is not int
            or identity["pid"] <= 0 or not str(identity.get("start_ticks", "")).isdecimal()
            or not isinstance(identity.get("boot_id"), str) or not identity["boot_id"]):
        raise SessionError("The launcher identity is unknown; automatic recovery is unavailable")
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    if identity["boot_id"] != boot:
        return False
    try:
        fields = Path(f"/proc/{identity['pid']}/stat").read_text().rsplit(")", 1)[1].split()
    except FileNotFoundError:
        return False
    return fields[19] == str(identity["start_ticks"]) and fields[0] != "Z"


def inspect(state, unit, env):
    if not UNIT.fullmatch(unit):
        raise SessionError("The unit is not a managed session service")
    record = read_record(state / f"{unit}.json")
    if (record.get("schema") != "skybuild.managed-session.v1" or record.get("unit") != unit
            or record.get("host", "").casefold() != socket.gethostname().casefold()
            or record.get("profile") != UNIT.fullmatch(unit).group(1)):
        raise SessionError("The launch record has a different service identity")
    if record["profile"] == "merge" and not re.fullmatch(r"refs/heads/dev-\d{3}", record.get("target_ref") or ""):
        raise SessionError("The launch record has no exact merge target")
    props = dict(line.split("=", 1) for line in checked(
        ["systemctl", "--user", "show", unit, "-p", "LoadState", "-p", "ActiveState",
         "-p", "InvocationID", "-p", "ControlGroup"], env=env).splitlines() if "=" in line)
    terminal = props.get("LoadState") == "not-found" or (
        props.get("LoadState") == "loaded" and props.get("ActiveState") in {"inactive", "failed"})
    result_path = state / f"{unit}.result.json"
    result = read_record(result_path) if result_path.is_file() else None
    cleanup = False
    invocation_matches = False
    if result is not None:
        invocation = result.get("invocation_id")
        if (result.get("unit") != unit or not isinstance(invocation, str)
                or not INVOCATION.fullmatch(invocation) or invocation == "0" * 32
                or not isinstance(result.get("control_group"), str) or not result["control_group"]):
            raise SessionError("The cleanup record has no valid service identity")
        invocation_matches = props.get("LoadState") == "not-found" or props.get("InvocationID") == invocation
        cleanup = result.get("cleanup_confirmed") is True and result.get("remaining_process_count") == 0
    groups = {value for value in (props.get("ControlGroup"), (result or {}).get("control_group")) if value}
    remaining = False
    for group in groups:
        if not isinstance(group, str) or not group.startswith("/") or ".." in Path(group).parts or Path(group).name != unit:
            raise SessionError("The control group has a different service identity")
        for counter in (Path("/sys/fs/cgroup") / group.lstrip("/")).rglob("cgroup.procs"):
            remaining = remaining or bool(counter.read_text().strip())
    try:
        live = launcher_alive(record)
    except SessionError:
        live = None
    summary = {"unit": unit, "profile": record["profile"], "target_ref": record.get("target_ref"),
               "load_state": props.get("LoadState"), "active_state": props.get("ActiveState"),
               "launcher_alive": live, "cleanup_confirmed": cleanup,
               "invocation_matches": invocation_matches, "remaining_processes": remaining,
               "physical_exit_confirmed": terminal and cleanup and invocation_matches and not remaining}
    return summary, record, result


def state_check(state):
    if not state.is_absolute() or state.is_symlink():
        raise SessionError("The state path must be an absolute directory without a symbolic link")
    if not state.exists():
        return False
    private_directory(state)
    return True


def status(state, env):
    if not state_check(state):
        return {"host": socket.gethostname(), "state_available": False, "merge_slot": None, "services": []}
    records = sorted(state.glob("skybuild-*.service.json"))
    if len(records) > 64:
        raise SessionError("The state inventory exceeds 64 records")
    summaries = []
    for path in records:
        unit = path.name.removesuffix(".json")
        try:
            summaries.append(inspect(state, unit, env)[0])
        except (SessionError, OSError, ValueError):
            summaries.append({"unit": unit, "status": "unknown"})
    slot_path = state / "merge-slot.json"
    slot = read_record(slot_path) if slot_path.exists() else None
    return {"host": socket.gethostname(), "state_available": True, "merge_slot": slot, "services": summaries}


def resolution_bytes(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise SessionError("The resolution evidence must be a private regular file owned by this user")
        content = stream.read(16385)
    if len(content) > 16384:
        raise SessionError("The resolution evidence is too large")
    return content


def recover(state, unit, evidence_path, env):
    if not state_check(state):
        raise SessionError("The state directory does not exist")
    if not evidence_path.is_absolute():
        raise SessionError("The resolution evidence path must be absolute")
    descriptor = os.open(state / "admission.lock", os.O_RDWR | os.O_NOFOLLOW)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        summary, record, result = inspect(state, unit, env)
        if summary["profile"] != "merge":
            raise SessionError("Only a merge service can own the merge slot")
        if summary["launcher_alive"] is not False:
            raise SessionError("The original launcher is alive or its identity is unknown")
        if not summary["physical_exit_confirmed"]:
            raise SessionError("The service exit and cleanup are not confirmed")
        # Bind the receipt to the exact bytes that supplied the decision.
        evidence_bytes = resolution_bytes(evidence_path)
        evidence = json.loads(evidence_bytes)
        if not isinstance(evidence, dict):
            raise SessionError("The resolution evidence is not an object")
        if (evidence.get("schema") != "skybuild.merge-resolution.v1" or evidence.get("unit") != unit
                or evidence.get("target_ref") != record.get("target_ref")
                or evidence.get("invocation_id") != result["invocation_id"]
                or evidence.get("publication_state") not in {"not-started", "confirmed", "no-effect"}
                or not isinstance(evidence.get("reason"), str) or not evidence["reason"].strip()
                or type(evidence.get("observed_at")) not in {int, float}
                or type(result.get("finished_at")) not in {int, float}
                or not result["finished_at"] <= evidence["observed_at"] <= time.time()):
            raise SessionError("Publication is not resolved for the exact service, invocation, and target")
        digest = hashlib.sha256(evidence_bytes).hexdigest()
        history = state / "recoveries"
        private_directory(history)
        receipt_path = history / f"{unit}.json"
        slot_path = state / "merge-slot.json"
        slot = read_record(slot_path) if slot_path.exists() else None
        owns_slot = slot is not None and slot.get("unit") == unit and slot.get("target_ref") == record["target_ref"]
        if receipt_path.exists():
            receipt = read_record(receipt_path)
            if (receipt.get("unit") != unit or receipt.get("invocation_id") != result["invocation_id"]
                    or receipt.get("resolution_sha256") != digest):
                raise SessionError("The retained recovery receipt has different evidence")
            if not owns_slot:
                return receipt
        else:
            if slot is not None and not owns_slot:
                raise SessionError("The merge slot has a different owner; no slot was released")
            if slot is None:
                raise SessionError("This service has no merge slot or prior recovery receipt")
            receipt = {"schema": "skybuild.merge-recovery.v1", "unit": unit,
                       "invocation_id": result["invocation_id"], "target_ref": record["target_ref"],
                       "publication_state": evidence["publication_state"], "resolution_sha256": digest,
                       "recovered_at": time.time()}
            write_new(receipt_path, receipt)
        if slot is not None:
            slot_path.unlink()
            directory = os.open(state, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        return receipt
    finally:
        os.close(descriptor)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-host", required=True)
    parser.add_argument("--state-dir", type=Path, default=Path.home() / "my_code/skybuild-managed-state")
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("status")
    recovery = commands.add_parser("recover")
    recovery.add_argument("--unit", required=True)
    recovery.add_argument("--resolution-evidence", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        env = service_environment(args.expected_host)
        value = status(args.state_dir, env) if args.action == "status" else recover(
            args.state_dir, args.unit, args.resolution_evidence, env)
        print(json.dumps(value, sort_keys=True))
        return 0
    except (SessionError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"managed_session_state: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
