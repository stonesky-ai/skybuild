#!/usr/bin/env python3
"""Trusted preflight that runs inside the candidate test container."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import socket
import sys
import time


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


def main() -> int:
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
    shutil.copytree(source, workspace, symlinks=True)
    print("GATE_CANDIDATE_COPY=complete", flush=True)
    for relative in ("home", "tmp", "uv-cache", "pytest-tmp", "pytest-cache"):
        (Path("/scratch") / relative).mkdir(mode=0o700)
    os.chdir(workspace)
    sys.path.insert(0, str(workspace / "src"))
    import skybuild

    expected = (workspace / "src/skybuild/__init__.py").resolve()
    actual = Path(skybuild.__file__).resolve()
    if actual != expected:
        raise RuntimeError("candidate package import resolved outside its scratch copy")
    print(f"GATE_PREFLIGHT_IMPORT={actual}", flush=True)
    _network_preflight()
    argv = ["uv", "run", "--extra", "test", "python", "-m", "pytest", "-q"]
    os.execvpe(argv[0], argv, os.environ.copy())
    return 127


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"GATE_PREFLIGHT_ERROR={type(error).__name__}", file=sys.stderr, flush=True)
        raise SystemExit(125)
