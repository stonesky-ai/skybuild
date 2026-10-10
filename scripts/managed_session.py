#!/usr/bin/env python3
"""Run an owner-authorized command in a bounded systemd user service.

Use one private state directory on each host. A retained launch record blocks
another merge run after an unknown result. This helper does not select tasks,
approve reviews, publish code, or grant permission to use an inference model.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import sys
import time
from uuid import uuid4

GIB = 1024**3
RESERVE = 8 * GIB
MAX_RECORD_BYTES = 16384


class SessionError(RuntimeError):
    """The service cannot start or its result needs inspection."""


def checked(argv, *, cwd=None, env=None):
    result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True,
                            timeout=15, check=False)
    if result.returncode:
        raise SessionError(f"Required command failed: {Path(argv[0]).name} (exit {result.returncode})")
    return result.stdout.strip()


def private_directory(path):
    if not path.is_absolute() or path.is_symlink():
        raise SessionError("The state path must be an absolute directory without a symbolic link")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise SessionError("The state directory must have this user as owner and mode 0700")


def write_new(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_record(path):
    if path.is_symlink() or path.stat().st_size > MAX_RECORD_BYTES:
        raise SessionError("The launch record is unsafe or too large")
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise SessionError("The launch record is not an object")
    return data


def available_memory():
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) * 1024
    raise SessionError("Available memory cannot be measured")


def validate(args):
    if socket.gethostname().casefold() != args.expected_host.casefold():
        raise SessionError("The execution host differs from the required host")
    root = args.checkout
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise SessionError("The checkout must be an existing absolute directory")
    if Path(checked(["git", "rev-parse", "--show-toplevel"], cwd=root)).resolve() != root.resolve():
        raise SessionError("The checkout differs from the Git root")
    allowed = {"https://github.com/stonesky-ai/skybuild", "git@github.com:stonesky-ai/skybuild",
               "ssh://git@github.com/stonesky-ai/skybuild"}
    for option in ([], ["--push"]):
        urls = checked(["git", "remote", "get-url", *option, "--all", "origin"], cwd=root).splitlines()
        if not urls or any(url.removesuffix(".git").rstrip("/") not in allowed for url in urls):
            raise SessionError("The origin is not the SkyBuild repository")
    if not 1 <= args.memory_high_gib < args.memory_max_gib <= 16:
        raise SessionError("MemoryHigh must be below MemoryMax; MemoryMax must not exceed 16 GiB")
    if not 1 <= args.runtime_seconds <= 8 * 3600:
        raise SessionError("The runtime must be between one second and eight hours")
    if not args.command or not Path(args.command[0]).is_absolute():
        raise SessionError("The command must start with an absolute executable path")
    if args.profile == "merge":
        if not args.target_ref or not re.fullmatch(r"refs/heads/dev-\d{3}", args.target_ref):
            raise SessionError("A merge run requires an exact refs/heads/dev-NNN target")
        # The runner must receive and check the same target. Do not use a CLI default.
        if args.target_ref not in args.command and args.target_ref.removeprefix("refs/heads/") not in args.command:
            raise SessionError("The final command arguments must contain the exact target ref or branch")
    elif args.target_ref is not None:
        raise SessionError("An analysis run does not accept a publication target")
    runtime = Path(f"/run/user/{os.getuid()}")
    if not (runtime / "bus").is_socket():
        raise SessionError("The user service bus is not available")
    if not Path("/sys/fs/cgroup/cgroup.controllers").is_file():
        raise SessionError("The host does not use cgroup v2")
    env = dict(os.environ, XDG_RUNTIME_DIR=str(runtime),
               DBUS_SESSION_BUS_ADDRESS=f"unix:path={runtime}/bus")
    checked(["systemctl", "--user", "show", "-p", "Version"], env=env)
    return env


def service_command(args, unit):
    # The main command's exit stops the service and all remaining descendants.
    props = ["Type=exec", "ExitType=main", "RemainAfterExit=no", "KillMode=control-group",
             "TimeoutStopSec=10", "SendSIGKILL=yes", "OOMPolicy=kill", "MemoryAccounting=yes",
             f"MemoryHigh={args.memory_high_gib * GIB}", f"MemoryMax={args.memory_max_gib * GIB}",
             "MemorySwapMax=0", f"RuntimeMaxSec={args.runtime_seconds}", "NoNewPrivileges=yes",
             f"WorkingDirectory={args.checkout}", f"Nice={10 if args.profile == 'analysis' else 0}",
             f"OOMScoreAdjust={300 if args.profile == 'analysis' else 0}"]
    hook = Path(__file__).with_name("managed_session_cleanup.py")
    hook_args = ["/usr/bin/python3", str(hook), str(args.state_dir / f"{unit}.json")]
    props.append("ExecStopPost=" + " ".join(shlex.quote(value.replace("%", "%%")) for value in hook_args))
    argv = ["systemd-run", "--user", "--quiet", "--wait", "--pipe", "--expand-environment=no",
            f"--unit={unit}"]
    for prop in props:
        argv += ["--property", prop]
    # Do not copy credentials from the launcher's environment into unit arguments.
    environment = {key: os.environ[key] for key in ("HOME", "USER", "LOGNAME", "LANG", "TERM", "PATH")
                   if key in os.environ}
    environment["UV_CACHE_DIR"] = str(args.state_dir / "uv-cache")
    environment["SKYBUILD_SESSION_UNIT"] = unit
    return argv + ["--", "/usr/bin/env", "-i", *[f"{key}={value}" for key, value in environment.items()],
                   *args.command]


def run(args):
    env = validate(args)
    private_directory(args.state_dir)
    lock_path = args.state_dir / "admission.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    unit = f"skybuild-{args.profile}-{uuid4().hex}.service"
    record_path = args.state_dir / f"{unit}.json"
    slot_path = args.state_dir / "merge-slot.json"
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        if args.profile == "merge" and slot_path.exists():
            raise SessionError("The merge slot is occupied; inspect the retained service before another run")
        exposure = 0
        records = list(args.state_dir.glob("skybuild-*.service.json"))
        if len(records) > 64:
            raise SessionError("The state inventory exceeds 64 records; archive completed records first")
        for path in records:
            previous = read_record(path)
            result_path = args.state_dir / f"{previous.get('unit')}.result.json"
            completed = False
            if result_path.is_file():
                prior_result = read_record(result_path)
                completed = (prior_result.get("unit") == previous.get("unit")
                             and prior_result.get("cleanup_confirmed") is True)
            if not completed:
                cap = previous.get("memory_max_bytes")
                if type(cap) is not int or not 0 < cap <= 16 * GIB:
                    raise SessionError("A retained memory limit is unknown")
                exposure += cap
        required = RESERVE + args.memory_max_gib * GIB + exposure
        if available_memory() < required:
            raise SessionError("Available memory is below the reserve plus all declared job limits")
        record = {"schema": "skybuild.managed-session.v1", "unit": unit, "host": socket.gethostname(),
                  "profile": args.profile, "checkout": str(args.checkout), "target_ref": args.target_ref,
                  "memory_max_bytes": args.memory_max_gib * GIB, "created_at": time.time(),
                  "phase": "launch_intent"}
        write_new(record_path, record)
        if args.profile == "merge":
            write_new(slot_path, {"unit": unit, "target_ref": args.target_ref})
    finally:
        os.close(descriptor)
    # A lost launch reply retains both intent and merge ownership. Never retry here.
    result = subprocess.run(service_command(args, unit), env=env, check=False)
    props = dict(line.split("=", 1) for line in checked(
        ["systemctl", "--user", "show", unit, "-p", "LoadState", "-p", "ActiveState", "-p", "SubState",
         "-p", "MemoryPeak", "-p", "ControlGroup", "-p", "Result", "-p", "ExecMainStatus"], env=env
    ).splitlines() if "=" in line)
    if props.get("LoadState") not in {"loaded", "not-found"} or (
            props.get("LoadState") == "loaded" and props.get("ActiveState") not in {"inactive", "failed"}):
        raise SessionError("The service has no confirmed terminal state; the launch record remains active")
    group = props.get("ControlGroup")
    if group:
        if not group.startswith("/") or ".." in Path(group).parts:
            raise SessionError("The service has an invalid control group")
        group_path = Path("/sys/fs/cgroup") / group.lstrip("/")
        for path in group_path.rglob("cgroup.procs"):
            if path.read_text().strip():
                raise SessionError("The service still has child processes; the launch record remains active")
    evidence = read_record(args.state_dir / f"{unit}.result.json")
    if evidence.get("unit") != unit or evidence.get("cleanup_confirmed") is not True:
        raise SessionError("The service has no confirmed cleanup record")
    if args.profile == "merge":
        descriptor = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            if read_record(slot_path).get("unit") != unit:
                raise SessionError("The merge slot has a different owner")
            slot_path.unlink()
        finally:
            os.close(descriptor)
    return result.returncode


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("analysis", "merge"), required=True)
    parser.add_argument("--expected-host", required=True)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, default=Path.home() / "my_code/skybuild-managed-state")
    parser.add_argument("--target-ref")
    parser.add_argument("--memory-high-gib", type=int, default=3)
    parser.add_argument("--memory-max-gib", type=int, default=4)
    parser.add_argument("--runtime-seconds", type=int, default=3600)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command[:1] == ["--"]:
        args.command.pop(0)
    try:
        return run(args)
    except (SessionError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"managed_session: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
