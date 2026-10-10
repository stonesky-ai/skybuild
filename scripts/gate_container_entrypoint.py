#!/usr/bin/env -S python3 -I
"""Trusted preflight that runs inside the candidate test container."""
from __future__ import annotations

import os
import calendar
import hashlib
from importlib.machinery import PathFinder
import json
from pathlib import Path
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
import tomllib


UV_VERSION = "0.11.22"
MAX_ENVIRONMENT_BYTES = 256 * 1024 * 1024
FULL_COMMAND = ["uv", "run", "--extra", "test", "python", "-m", "pytest", "-q"]
FOCUSED_COMMANDS = {
    "petri-client-unit-v1": FULL_COMMAND + ["tests/test_petri_client.py"],
    "petri-client-long-v1": FULL_COMMAND + ["tests/test_petri_workers.py", "tests/test_manual_cord.py"],
    "session-scan-unit-v1": FULL_COMMAND + ["tests/test_session_failure_scan.py", "-k",
                                           "reassembles or prefilter or concatenated or keeps_complete or quoted"],
    "session-scan-long-v1": FULL_COMMAND + ["tests/test_session_failure_scan.py", "-k",
                                           "not (reassembles or prefilter or concatenated or keeps_complete or quoted)"],
}


def fixed_command(arguments: list[str] | None) -> list[str]:
    if arguments is None or arguments == FULL_COMMAND:
        return list(FULL_COMMAND)
    for command in FOCUSED_COMMANDS.values():
        if arguments == command:
            return list(command)
    raise RuntimeError("container command is outside the reviewed fixed whitelist")


def _prepare_environment(workspace: Path, template: Path) -> None:
    """Copy reviewed installed dependencies and rebase editable metadata as data."""
    project_bytes = (workspace / "pyproject.toml").read_bytes()
    project = tomllib.loads(project_bytes.decode())
    if (project.get("project", {}).get("dynamic")
            or "cache-keys" in project.get("tool", {}).get("uv", {})
            or (workspace / "setup.py").exists() or (workspace / "setup.cfg").exists()):
        raise RuntimeError("project cache metadata requires a separately qualified image contract")
    manifest = json.loads((template / ".skybuild-environment.json").read_bytes())
    expected = {"schema": "skybuild.full-test.environment.v1", "uv_version": UV_VERSION,
                "pyproject_sha256": hashlib.sha256(project_bytes).hexdigest(),
                "uv_lock_sha256": hashlib.sha256((workspace / "uv.lock").read_bytes()).hexdigest()}
    if manifest != expected:
        raise RuntimeError("preinstalled environment does not match the pinned project and uv protocol")
    size = 0
    count = 0
    for path in template.rglob("*"):
        info = path.lstat()
        count += 1
        if stat.S_ISREG(info.st_mode):
            size += info.st_size
        elif not (stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)):
            raise RuntimeError("preinstalled environment contains a special file")
        if size > MAX_ENVIRONMENT_BYTES or count > 100_000:
            raise RuntimeError("preinstalled environment exceeds its copy bound")
    destination = workspace / ".venv"
    if destination.exists():
        raise RuntimeError("candidate archive contains an unexpected project environment")
    shutil.copytree(template, destination, symlinks=True)
    metadata = list(destination.glob("lib/python*/site-packages/skybuild-*.dist-info"))
    pth = list(destination.glob("lib/python*/site-packages/_editable_impl_skybuild.pth"))
    if (len(metadata) != 1 or len(pth) != 1 or not (destination / "bin/python").is_file()
            or json.loads((metadata[0] / "uv_build.json").read_bytes()) != {}):
        raise RuntimeError("preinstalled editable project metadata is incomplete")
    # The immutable image contains reviewed installed metadata for this exact
    # pyproject hash. Rebase its source URL/path without loading a build backend.
    (metadata[0] / "direct_url.json").write_text(json.dumps({
        "url": workspace.as_uri(), "dir_info": {"editable": True}}), encoding="utf-8")
    pth[0].write_text(str(workspace / "src"), encoding="utf-8")
    # uv 0.11.22 keys Unix files by ctime and directories by creation time,
    # falling back to inode when creation time is unavailable. Copying the
    # reviewed environment changes these keys without changing project bytes.
    changed = (workspace / "pyproject.toml").stat().st_ctime_ns
    seconds, nanos = divmod(changed, 1_000_000_000)
    birth = subprocess.run(["/usr/bin/stat", "--format=%w", str(workspace / "src")],
        capture_output=True, text=True, timeout=5, check=True,
        env={"LC_ALL": "C", "TZ": "UTC"}).stdout.strip()
    if birth == "-":
        directory = (workspace / "src").stat().st_ino
    else:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{9} \+0000", birth):
            raise RuntimeError("directory creation time has an unsupported format")
        directory = {"secs_since_epoch": calendar.timegm(time.strptime(birth[:19], "%Y-%m-%d %H:%M:%S")),
                     "nanos_since_epoch": int(birth[20:29])}
    (metadata[0] / "uv_cache.json").write_text(json.dumps({
        "timestamp": {"secs_since_epoch": seconds, "nanos_since_epoch": nanos},
        "commit": None, "tags": None, "env": {}, "directories": {"src": directory}}), encoding="utf-8")
    print("GATE_OFFLINE_ENVIRONMENT=prepared", flush=True)


def _can_connect(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.35):
            return True
    except (OSError, TimeoutError):
        return False


def _network_preflight() -> None:
    gateway = os.environ.get("SKYBUILD_GATE_HOST_GATEWAY", "")
    listener_port = int(os.environ.get("SKYBUILD_GATE_HOST_LISTENER_PORT", "0"))
    if not gateway:
        raise RuntimeError("missing reviewed internal-network gateway")
    if not listener_port or _can_connect(gateway, listener_port):
        raise RuntimeError("network isolation probe reached a host or external endpoint")
    if _can_connect("1.1.1.1", 443):
        raise RuntimeError("network isolation probe reached external IPv4")
    if _can_connect("2001:4860:4860::8888", 443):
        raise RuntimeError("network isolation probe reached external IPv6")
    if not _dns_is_blocked():
        raise RuntimeError("network isolation probe reached Docker DNS")
    print("GATE_HOST_GATEWAY_PROBE=blocked", flush=True)
    print("GATE_EXTERNAL_DIRECT_IP_PROBE=blocked", flush=True)
    print("GATE_EXTERNAL_DNS_PROBE=blocked", flush=True)


def _dns_is_blocked() -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(0.5)
    try:
        question = (b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
                    b"\x07example\x03com\x00\x00\x01\x00\x01")
        sock.sendto(question, ("127.0.0.11", 53))
        try:
            sock.recvfrom(512)
        except TimeoutError:
            return True
        return False
    except OSError:
        return True
    finally:
        sock.close()


def main(arguments: list[str] | None = None) -> int:
    argv = fixed_command(arguments)
    release_file = Path(os.environ.get("SKYBUILD_GATE_RELEASE_FILE", ""))
    if release_file != Path("/scratch/.firewall-ready"):
        raise RuntimeError("reviewed firewall release path is missing")
    print("GATE_CANDIDATE_BLOCKED=firewall_release_pending", flush=True)
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if release_file.is_file():
            if release_file.read_text(encoding="ascii") != "firewall-ready\n":
                raise RuntimeError("firewall release marker is invalid")
            break
        time.sleep(0.1)
    else:
        raise RuntimeError("network firewall was not installed before candidate startup")
    print("GATE_FIREWALL_RELEASE=verified", flush=True)
    source = Path("/candidate")
    workspace = Path("/scratch/workspace")
    if not (source / "src/skybuild/__init__.py").is_file() or not (source / "tests").is_dir():
        raise RuntimeError("readonly candidate archive is incomplete")
    write_probe = source / ".skybuild-readonly-probe"
    try:
        write_probe.write_text("must not be writable", encoding="utf-8")
    except OSError:
        print("GATE_CANDIDATE_ARCHIVE_READONLY=true", flush=True)
    else:
        write_probe.unlink(missing_ok=True)
        raise RuntimeError("candidate archive mount unexpectedly permits writes")
    if workspace.exists():
        raise RuntimeError("scratch workspace unexpectedly exists")
    shutil.copytree(source, workspace, symlinks=True, ignore=shutil.ignore_patterns(".git"))
    if not (source / ".git/HEAD").is_file():
        raise RuntimeError("sanitized readonly Git history fixture is missing")
    (workspace / ".git").symlink_to(source / ".git", target_is_directory=True)
    print("GATE_CANDIDATE_COPY=complete", flush=True)
    for relative in ("home", "tmp", "uv-cache", "pytest-tmp", "pytest-cache"):
        (Path("/scratch") / relative).mkdir(mode=0o700)
    (Path("/scratch/home") / ".gitconfig").write_text(
        "[safe]\n\tdirectory = /scratch/workspace\n", encoding="ascii")
    _prepare_environment(workspace, Path("/opt/skybuild-venv"))
    os.chdir(workspace)
    # PathFinder inspects filenames only. It must never execute candidate code
    # before this trusted launcher reaches the fixed command exec boundary.
    spec = PathFinder.find_spec("skybuild", [str(workspace / "src")])
    expected = (workspace / "src/skybuild/__init__.py").resolve()
    if spec is None or spec.origin is None or Path(spec.origin).resolve() != expected:
        raise RuntimeError("candidate package source resolved outside its scratch copy")
    print(f"GATE_PREFLIGHT_SOURCE_PATH={expected}", flush=True)
    _network_preflight()
    print("GATE_COMMAND_LAUNCH=trusted_exec", flush=True)
    os.execvpe(argv[0], argv, os.environ.copy())
    return 127


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except Exception as error:
        print(f"GATE_PREFLIGHT_ERROR={type(error).__name__}", file=sys.stderr, flush=True)
        raise SystemExit(125)
