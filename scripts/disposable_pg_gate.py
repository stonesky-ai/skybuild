#!/usr/bin/env python3
"""Run the project gate against three newly created, task-owned PostgreSQL databases."""
from __future__ import annotations

import argparse
from _repo_guard import verify_skybuild, RepoGuardError
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import tempfile
import time


def available_memory_bytes() -> int:
    try:
        status = Path("/proc/meminfo").read_text(encoding="ascii")
    except OSError as error:
        raise RuntimeError("Available memory cannot be measured") from error
    match = re.search(r"^MemAvailable:\s+(\d+) kB$", status, re.MULTILINE)
    if not match:
        raise RuntimeError("Available memory cannot be measured")
    return int(match[1]) * 1024


def run_gate(checkout: Path, timeout: float, image: str, command: list[str],
             min_available_bytes: int = 0) -> tuple[dict, int]:
    name = "skybuild-gate-" + secrets.token_hex(8)
    password = secrets.token_urlsafe(32)
    descriptor, log_path = tempfile.mkstemp(prefix=name + "-", suffix=".log")
    result = {"ok": False, "log": log_path, "container": name}
    exit_code = 1
    started = False
    deadline = time.monotonic() + timeout

    def interrupt(signum, frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, interrupt)
    with os.fdopen(descriptor, "w+", encoding="utf-8") as log:
        def execute(argv, *, capture=False, env=None, check=True):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Gate deadline exceeded")
            completed = subprocess.run(argv, cwd=checkout, env=env, text=True,
                                       stdout=subprocess.PIPE if capture else log,
                                       stderr=log, timeout=remaining, check=check)
            return completed

        try:
            def require_headroom():
                if min_available_bytes and available_memory_bytes() < min_available_bytes:
                    raise RuntimeError("Available memory is below the gate minimum")

            require_headroom()
            env = dict(os.environ, POSTGRES_PASSWORD=password)
            # The random loopback binding and anonymous volume belong only to this container.
            started = True
            execute(["docker", "run", "--detach", "--name", name,
                     "--label", "skybuild.disposable-gate=true", "--memory", "512m",
                     "--memory-swap", "512m", "--pids-limit", "128", "--publish", "127.0.0.1::5432",
                     "--env", "POSTGRES_PASSWORD", image], env=env)
            while execute(["docker", "exec", name, "pg_isready", "-h", "127.0.0.1", "-U", "postgres"], check=False).returncode:
                time.sleep(min(0.25, max(0, deadline - time.monotonic())))
            binding = execute(["docker", "port", name, "5432/tcp"], capture=True).stdout.strip()
            match = re.fullmatch(r"127\.0\.0\.1:(\d+)", binding)
            if not match:
                raise RuntimeError("Container port must bind only to localhost")
            port = int(match[1])
            test_env = os.environ.copy()
            # Never inherit a live application DSN into the gate.
            test_env.pop("SKYBUILD_DSN", None)
            test_env.pop("SKYBUILD_ROLE_ADMIN_DSN", None)
            for variable, database in (("SKYBUILD_TEST_DSN", "skybuild_test"),
                                       ("SKYBUILD_HTTP_TEST_DSN", "skybuild_http_test"),
                                       ("SKYBUILD_IMPORT_TEST_DSN", "skybuild_import_test")):
                execute(["docker", "exec", name, "createdb", "-U", "postgres", database])
                test_env[variable] = f"postgresql://postgres:{password}@127.0.0.1:{port}/{database}"
            require_headroom()
            exit_code = execute(command, env=test_env, check=False).returncode
            result["ok"] = exit_code == 0
        except (OSError, RuntimeError, subprocess.SubprocessError, TimeoutError, KeyboardInterrupt) as error:
            # Do not expose subprocess environment or database passwords in the summary.
            result["error"] = type(error).__name__
            log.write(f"\nGate failed: {type(error).__name__}\n")
        finally:
            if started:
                try:
                    cleanup = subprocess.run(["docker", "rm", "--force", "--volumes", name],
                                             stdout=log, stderr=log, timeout=30, check=False)
                    result["cleaned_up"] = cleanup.returncode == 0
                except (OSError, subprocess.SubprocessError):
                    result["cleaned_up"] = False
                if not result["cleaned_up"]:
                    result["ok"] = False
                    exit_code = 1
            signal.signal(signal.SIGTERM, previous)
            log.flush()
            log.seek(0)
            output = log.read().replace(password, "[REDACTED]")
            log.seek(0)
            log.write(output)
            log.truncate()
            summaries = re.findall(r"^.*(?:\d+ passed|\d+ failed|\d+ skipped).*$", output, re.MULTILINE)
            if summaries:
                result["pytest_summary"] = summaries[-1].strip("= ")
    result["exit_code"] = exit_code
    return result, exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--timeout", type=float, default=600, help="Total gate deadline in seconds")
    parser.add_argument("--image", default="postgres:16", help="Disposable PostgreSQL Docker image")
    parser.add_argument("--min-available-gib", type=int, default=0,
                        help="Refuse to start PostgreSQL or tests below this available-memory threshold")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Optional command argv after --")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.min_available_gib < 0:
        parser.error("--min-available-gib must not be negative")
    try:
        checkout = verify_skybuild(args.checkout.resolve())
    except RepoGuardError as error:
        parser.error(str(error))
    if not (checkout / "pyproject.toml").is_file():
        parser.error("--checkout must name a project containing pyproject.toml")
    command = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    result, code = run_gate(checkout, args.timeout, args.image,
                            command or ["uv", "run", "--extra", "test", "python", "-m", "pytest", "-q"],
                            min_available_bytes=args.min_available_gib * 1024**3)
    print(json.dumps(result, sort_keys=True))
    return code if 0 <= code <= 125 else 1


if __name__ == "__main__":
    raise SystemExit(main())
