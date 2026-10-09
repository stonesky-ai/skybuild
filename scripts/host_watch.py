#!/usr/bin/env python3
"""Sample host hygiene into one atomic state file without model polling."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import time

from memory_capacity import observe as observe_memory_capacity


def disk_status(paths: list[Path], reserve_bytes: int) -> list[dict]:
    result = []
    for path in paths:
        try:
            free = shutil.disk_usage(path).free
            result.append({"path": str(path), "status": "ok" if free >= reserve_bytes else "low",
                           "free_bytes": free, "reserve_bytes": reserve_bytes})
        except OSError:
            result.append({"path": str(path), "status": "unknown", "reserve_bytes": reserve_bytes})
    return result


def docker_gate_status() -> dict:
    try:
        daemon = subprocess.run(["systemctl", "is-active", "--quiet", "docker.service"],
                                capture_output=True, text=True, timeout=5, check=False)
        if daemon.returncode:
            return {"status": "unknown", "error": "docker_service_inactive"}
        listed = subprocess.run(
            ["docker", "ps", "-a", "--filter", "label=skybuild.disposable-gate=true", "--format", "{{.ID}}"],
            capture_output=True, text=True, timeout=5, check=True,
        )
        ids = listed.stdout.splitlines()
        if not ids:
            return {"status": "clear", "count": 0, "leftover_count": 0}
        if len(ids) > 32:
            return {"status": "unknown", "count": len(ids), "error": "too_many_owned_containers"}
        inspected = subprocess.run(
            ["docker", "inspect", "--type", "container", "--format",
             "{{json .Name}}\t{{json .Created}}\t{{json .State.Status}}", *ids],
            capture_output=True, text=True, timeout=5, check=True,
        )
        now = datetime.now(timezone.utc)
        containers = []
        for identifier, line in zip(ids, inspected.stdout.splitlines(), strict=True):
            name, created, status = (json.loads(value) for value in line.split("\t"))
            age = max(0, int((now - datetime.fromisoformat(created.replace("Z", "+00:00"))).total_seconds()))
            containers.append({"id": identifier, "name": name.removeprefix("/"),
                               "state": status, "age_seconds": age,
                               "leftover": status != "running" or age > 20 * 60})
        leftovers = sum(item["leftover"] for item in containers)
        return {"status": "attention" if leftovers else "present", "count": len(containers),
                "leftover_count": leftovers, "containers": containers}
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return {"status": "unknown", "error": "docker_inventory_unavailable"}


def docker_runtime_status() -> dict:
    """Observe only the owned pilot project; never retrieve container secrets."""
    try:
        listed = subprocess.run(
            ["docker", "ps", "-a", "--filter", "label=com.docker.compose.project=skybuild-pilot",
             "--format", "{{.ID}}"], capture_output=True, text=True, timeout=5, check=True,
        )
        ids = listed.stdout.splitlines()
        if not ids:
            return {"status": "absent", "count": 0}
        if len(ids) > 32 or any(not re.fullmatch(r"[0-9a-f]{12,64}", identifier) for identifier in ids):
            return {"status": "unknown", "error": "invalid_runtime_inventory"}
        inspected = subprocess.run(
            ["docker", "inspect", "--type", "container", "--format",
             '{{json .Name}}\t{{json .State.Status}}\t'
             '{{if .State.Health}}{{json .State.Health.Status}}{{else}}"none"{{end}}\t'
             '{{json .HostConfig.Memory}}\t{{json (index .Config.Labels "com.docker.compose.project")}}',
             *ids], capture_output=True, text=True, timeout=5, check=True,
        )
        containers = []
        for identifier, line in zip(ids, inspected.stdout.splitlines(), strict=True):
            name, state, health, memory, project = (json.loads(value) for value in line.split("\t"))
            if project != "skybuild-pilot" or not isinstance(name, str):
                raise ValueError("Runtime ownership changed or name is unavailable")
            known_state = state in {"created", "running", "paused", "restarting", "removing", "exited", "dead"}
            known_health = health in {"none", "starting", "healthy", "unhealthy"}
            known_cap = type(memory) is int and memory > 0
            status = ("unknown" if not (known_state and known_health and known_cap) else
                      "healthy" if state == "running" and health in {"none", "healthy"} else "failed")
            containers.append({"id": identifier,
                               "name": re.sub(r"[^A-Za-z0-9_.-]", "_", name.removeprefix("/"))[:128],
                               "state": state if known_state else "unknown",
                               "health": health if known_health else "unknown",
                               "memory_limit_bytes": memory if known_cap else None, "status": status})
        statuses = {container["status"] for container in containers}
        return {"status": "unknown" if "unknown" in statuses else "attention" if "failed" in statuses else "present",
                "count": len(containers), "containers": containers}
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return {"status": "unknown", "error": "runtime_inventory_unavailable"}


def worktree_status(checkout: Path) -> dict:
    try:
        listed = subprocess.run(["git", "-C", str(checkout), "worktree", "list", "--porcelain"],
                                capture_output=True, text=True, timeout=5, check=True)
    except (OSError, subprocess.SubprocessError):
        return {"status": "unknown", "error": "worktree_inventory_unavailable"}
    blocks = [block.splitlines() for block in listed.stdout.strip().split("\n\n") if block]
    if len(blocks) > 64:
        return {"status": "unknown", "count": len(blocks), "error": "too_many_worktrees"}
    records = []
    for block in blocks:
        fields = dict(line.partition(" ")[::2] for line in block)
        path = fields.get("worktree", "")
        records.append({"path": path, "head": fields.get("HEAD"), "branch": fields.get("branch"),
                        "prunable_reason": fields.get("prunable"),
                        "ownership": "checkout" if path == str(checkout.resolve()) else "unknown"})
    prunable = sum(item["prunable_reason"] is not None for item in records)
    return {"status": "attention" if prunable else "clear", "count": len(records),
            "prunable_count": prunable, "records": records}


def sample(reserve_bytes: int, *, disk_paths: list[Path] | None = None,
           disk_reserve_bytes: int = 4 * 1024**3, checkout: Path | None = None,
           jobs_registry: Path | None = None, capacity_state: Path | None = None,
           new_job_limit_bytes: int = 4 * 1024**3) -> dict:
    values = {}
    for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
        if line.startswith(("MemAvailable:", "SwapFree:")):
            name, amount, unit = line.split()
            if unit != "kB":
                raise ValueError("Unexpected memory unit")
            values[name.removesuffix(":")] = int(amount) * 1024
    if "MemAvailable" not in values:
        raise ValueError("MemAvailable is unavailable")
    available = values["MemAvailable"]
    result = {
        "sampled_at": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
        "status": "ok" if available >= reserve_bytes else "low",
        "available_bytes": available,
        "reserve_bytes": reserve_bytes,
        "headroom_bytes": available - reserve_bytes,
        "swap_free_bytes": values.get("SwapFree"),
    }
    if disk_paths is not None:
        result["disks"] = disk_status(disk_paths, disk_reserve_bytes)
        result["docker_gate"] = docker_gate_status()
        result["docker_runtime"] = docker_runtime_status()
    if checkout is not None:
        result["worktrees"] = worktree_status(checkout)
    observed = [item["status"] for item in result.get("disks", [])]
    observed += [result[key]["status"] for key in ("docker_gate", "docker_runtime", "worktrees") if key in result]
    if result["status"] != "low":
        result["status"] = ("low" if "low" in observed else "unknown" if "unknown" in observed
                            else "attention" if "attention" in observed else "ok")
    if jobs_registry is not None and capacity_state is not None:
        capacity_state.parent.mkdir(parents=True, exist_ok=True)
        lock = os.open(str(capacity_state) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                capacity = {"status": "unknown", "reason": "capacity_writer_active",
                            "sampled_at": result["sampled_at"], "max_new_jobs": 0}
            else:
                capacity = observe_memory_capacity(jobs_registry, capacity_state,
                                                   available_bytes=available, reserve_bytes=reserve_bytes,
                                                   new_job_limit_bytes=new_job_limit_bytes)
                write_state(capacity_state, capacity)
        finally:
            os.close(lock)
        result["capacity"] = {key: capacity[key] for key in
                              ("status", "sampled_at", "safe_total_jobs", "target_jobs", "max_new_jobs")
                              if key in capacity}
    return result


def write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(state, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def watch(path: Path, reserve_bytes: int, interval: int, duration_minutes: int,
          *, disk_paths: list[Path] | None = None, disk_reserve_bytes: int = 4 * 1024**3,
          checkout: Path | None = None, jobs_registry: Path | None = None,
          capacity_state: Path | None = None, new_job_limit_bytes: int = 4 * 1024**3) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    stop_path = path.with_name(path.name + ".stop")
    lock = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Memory watch already runs for this state file", flush=True)
            return 2

        def stop(signum, frame):
            raise SystemExit(0)

        previous = signal.signal(signal.SIGTERM, stop)
        deadline = time.monotonic() + duration_minutes * 60
        try:
            while time.monotonic() < deadline:
                if stop_path.exists():
                    break
                try:
                    state = sample(reserve_bytes, disk_paths=disk_paths,
                                   disk_reserve_bytes=disk_reserve_bytes, checkout=checkout,
                                   jobs_registry=jobs_registry, capacity_state=capacity_state,
                                   new_job_limit_bytes=new_job_limit_bytes)
                except (OSError, ValueError):
                    state = {"sampled_at": datetime.now(timezone.utc).isoformat(),
                             "pid": os.getpid(), "status": "unknown", "reserve_bytes": reserve_bytes}
                write_state(path, state)
                time.sleep(min(interval, max(0, deadline - time.monotonic())))
        finally:
            write_state(path, {"sampled_at": datetime.now(timezone.utc).isoformat(),
                               "pid": os.getpid(), "status": "stopped", "reserve_bytes": reserve_bytes})
            stop_path.unlink(missing_ok=True)
            signal.signal(signal.SIGTERM, previous)
    finally:
        os.close(lock)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, type=Path, help="Atomic JSON status path")
    parser.add_argument("--checkout", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--reserve-gib", type=int, default=8)
    parser.add_argument("--disk-reserve-gib", type=int, default=4)
    parser.add_argument("--disk-path", action="append", type=Path,
                        help="Disk path to check; defaults to checkout and /tmp")
    parser.add_argument("--watch", action="store_true", help="Sample repeatedly without model calls")
    parser.add_argument("--interval", type=int, default=60, help="Watch interval in seconds")
    parser.add_argument("--duration-minutes", type=int, default=480, help="Maximum watch lifetime")
    parser.add_argument("--jobs-registry", type=Path, help="Launcher-owned cgroup job registry JSON")
    parser.add_argument("--capacity-state", type=Path, help="Atomic persistent worker guidance JSON")
    parser.add_argument("--new-job-limit-gib", type=int, default=4,
                        help="Hard cgroup MemoryMax for each new job; must match launcher")
    args = parser.parse_args()
    if args.reserve_gib < 0 or args.disk_reserve_gib < 0:
        parser.error("Reserve must not be negative")
    if not 60 <= args.interval <= 90 or not 1 <= args.duration_minutes <= 480:
        parser.error("Watch interval must be 60–90 seconds and lifetime at most eight hours")
    if (args.jobs_registry is None) != (args.capacity_state is None):
        parser.error("Specify both --jobs-registry and --capacity-state")
    if args.new_job_limit_gib < 1 or args.new_job_limit_gib > 64:
        parser.error("New job MemoryMax must be 1–64 GiB")
    if args.capacity_state is not None and args.capacity_state.resolve() == args.state.resolve():
        parser.error("Capacity and host state files must differ")
    reserve_bytes = args.reserve_gib * 1024**3
    disk_paths = args.disk_path or [args.checkout, Path("/tmp")]
    disk_reserve_bytes = args.disk_reserve_gib * 1024**3
    capacity_args = {"jobs_registry": args.jobs_registry, "capacity_state": args.capacity_state,
                     "new_job_limit_bytes": args.new_job_limit_gib * 1024**3}
    if args.watch:
        return watch(args.state, reserve_bytes, args.interval, args.duration_minutes,
                     disk_paths=disk_paths, disk_reserve_bytes=disk_reserve_bytes, checkout=args.checkout,
                     **capacity_args)
    state = sample(reserve_bytes, disk_paths=disk_paths,
                   disk_reserve_bytes=disk_reserve_bytes, checkout=args.checkout, **capacity_args)
    write_state(args.state, state)
    print(json.dumps(state, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
