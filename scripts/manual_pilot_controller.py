"""Read-only controller preflight for the first manual REST/Cord worker pilot.

This deliberately does not provision a database, alter Tailscale Serve, or start
workers. The matching operator runbook contains the explicit deployment steps.
"""

import argparse
import json
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path


DATABASE_PORT = 55432
API_PORT = 8000
DATABASE_CONTAINER = "skybuild-pilot-pg"
POSTGRES_IMAGE = "postgres:16"
MIN_AVAILABLE_GIB = 10
MIN_DISK_GIB = 4


def _command(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, check=False, timeout=10)


def _available_gib() -> float:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / (1024 * 1024)
    raise ValueError("MemAvailable unavailable")


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def preflight(checkout: Path, expected_sha: str, published_ref: str) -> dict:
    """Return actionable checks without mutating the host or printing secrets."""
    checks: dict[str, dict] = {}

    def add(name: str, ok: bool, detail: str) -> None:
        checks[name] = {"ok": bool(ok), "detail": detail}

    checkout = checkout.resolve()
    git = _command("git", "-C", str(checkout), "rev-parse", "--show-toplevel")
    remote = _command("git", "-C", str(checkout), "remote", "get-url", "origin")
    push_remote = _command("git", "-C", str(checkout), "remote", "get-url", "--push", "origin")
    add("checkout", git.returncode == 0 and Path(git.stdout.strip()) == checkout
        and remote.stdout.strip() == "https://github.com/stonesky-ai/skybuild.git"
        and push_remote.stdout.strip() == remote.stdout.strip(),
        "Require exact SkyBuild checkout and matching origin fetch/push URLs")
    head = _command("git", "-C", str(checkout), "rev-parse", "HEAD")
    status = _command("git", "-C", str(checkout), "status", "--porcelain", "--untracked-files=all")
    published = _command("git", "-C", str(checkout), "ls-remote", "--exit-code", "origin", published_ref)
    pinned = bool(re.fullmatch(r"[0-9a-f]{40}", expected_sha)) and published_ref in {
        "refs/heads/dev-002", "refs/heads/main"}
    add("published_clean_head", pinned and head.returncode == 0 and head.stdout.strip() == expected_sha
        and status.returncode == 0 and not status.stdout
        and published.returncode == 0 and published.stdout.strip() == f"{expected_sha}\t{published_ref}",
        "Require clean checkout at the exact approved, published dev-002 or main SHA")

    available = _available_gib()
    add("memory", available >= MIN_AVAILABLE_GIB,
        f"{available:.1f} GiB available; require {MIN_AVAILABLE_GIB} GiB before setup")
    disk = shutil.disk_usage(checkout).free / (1024 ** 3)
    add("disk", disk >= MIN_DISK_GIB,
        f"{disk:.1f} GiB free at checkout; require {MIN_DISK_GIB} GiB")
    add("database_port", _port_free(DATABASE_PORT), f"127.0.0.1:{DATABASE_PORT} must be free")
    add("api_port", _port_free(API_PORT), f"127.0.0.1:{API_PORT} must be free")

    docker = _command("docker", "info", "--format", "{{.ServerVersion}}")
    add("docker", docker.returncode == 0, "Docker daemon must be available")
    compose = _command("docker", "compose", "version", "--short")
    add("compose", compose.returncode == 0, "Docker Compose plugin must be available")
    if docker.returncode == 0:
        image = _command("docker", "image", "inspect", POSTGRES_IMAGE, "--format", "{{.Id}}")
        add("postgres_image", image.returncode == 0,
            "Use locally available postgres:16 image; do not pull during setup")
        container = _command("docker", "container", "inspect", DATABASE_CONTAINER,
                             "--format", "{{.Id}}")
        add("container_name", container.returncode != 0,
            "Dedicated pilot container name must be unused; inspect existing container before retry")

    tailscale = _command("tailscale", "status", "--json")
    try:
        tail_status = json.loads(tailscale.stdout)
    except ValueError:
        tail_status = {}
    add("tailscale", tailscale.returncode == 0 and tail_status.get("BackendState") == "Running",
        "Tailscale must be running on controller")
    serve = _command("tailscale", "serve", "status", "--json")
    try:
        serve_status = json.loads(serve.stdout)
    except ValueError:
        serve_status = None
    add("serve_empty", serve.returncode == 0 and serve_status == {},
        "Existing Serve configuration requires manual ownership review before adding pilot route")

    return {"ready_for_operator_setup": all(row["ok"] for row in checks.values()),
            "checks": checks, "no_changes_made": True}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--published-ref", choices=("refs/heads/dev-002", "refs/heads/main"), required=True)
    args = parser.parse_args(argv)
    try:
        report = preflight(args.checkout, args.expected_sha, args.published_ref)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        report = {"ready_for_operator_setup": False, "checks": {}, "no_changes_made": True,
                  "error": "Preflight unavailable; inspect local dependencies"}
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0 if report["ready_for_operator_setup"] else 2


if __name__ == "__main__":
    sys.exit(main())
