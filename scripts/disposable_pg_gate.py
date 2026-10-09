#!/usr/bin/env python3
"""Run the project gate against three newly created, task-owned PostgreSQL databases."""
from __future__ import annotations

import argparse
from _repo_guard import verify_skybuild, RepoGuardError
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import tempfile
import time
from uuid import uuid4


class ArtifactError(RuntimeError):
    """Durable reporting failed; never interpret the gate as accepted."""


_SAFE_GATE_DIAGNOSTICS = {
    "Available memory cannot be measured",
    "Container port must bind only to localhost",
    "Gate deadline exceeded",
}


def _safe_gate_diagnostic(error: BaseException) -> str | None:
    detail = str(error)
    if detail in _SAFE_GATE_DIAGNOSTICS or re.fullmatch(
            r"Available memory below gate minimum: \d+ bytes available; \d+ bytes required", detail):
        return detail
    return None


def candidate_identity(checkout: Path) -> dict:
    def git(*args):
        return subprocess.check_output(["git", "-C", str(checkout), *args],
                                       stderr=subprocess.DEVNULL, timeout=10).decode().strip()
    if git("rev-parse", "--show-toplevel") != str(checkout) or git("status", "--porcelain", "--untracked-files=all"):
        raise ArtifactError("Candidate must be the clean checkout root")
    return {"head": git("rev-parse", "HEAD"), "tree": git("rev-parse", "HEAD^{tree}")}


class RunArtifact:
    """One exclusive run record; subsequent updates never adopt existing evidence."""
    def __init__(self, path, checkout, run_id, head, tree, *, timeout, deadline,
                 command, image, container, log, min_available_bytes):
        if (not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run_id)
                or not re.fullmatch(r"[0-9a-f]{40}", head or "")
                or not re.fullmatch(r"[0-9a-f]{40}", tree or "")
                or not math.isfinite(timeout) or timeout <= 0):
            raise ArtifactError("Explicit run and candidate pins are required")
        self.path = Path(path)
        self.checkout = checkout
        self.identity = {"head": head, "tree": tree}
        parent = self.path.parent
        info = parent.lstat()
        if (not self.path.is_absolute() or parent != parent.resolve()
                or not parent.is_dir() or info.st_uid != os.geteuid() or info.st_mode & 0o077
                or checkout == parent or checkout in parent.parents):
            raise ArtifactError("Artifact needs an external owned private directory")
        self.check_candidate()
        self.record = {"schema": "skybuild.gate-run.v1", "run_id": run_id,
                       "invocation_id": uuid4().hex, "pid": os.getpid(),
                       "checkout": str(checkout), **self.identity,
                       "started_at_unix": time.time(), "timeout_seconds": timeout,
                       "monotonic_deadline": deadline, "min_available_bytes": min_available_bytes,
                       "command_sha256": hashlib.sha256(json.dumps(command).encode()).hexdigest(),
                       "image_reference_sha256": hashlib.sha256(image.encode()).hexdigest(),
                       "container": container, "log": log, "phase": "prepared",
                       "status": "running", "cleanup": "not_started", "sequence": 0}
        payload = self.encode()
        # An incomplete initial write also reserves the path. Never repair or rerun it.
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        self.sync_directory()
        self.previous = payload

    def check_candidate(self):
        if candidate_identity(self.checkout) != self.identity:
            raise ArtifactError("Candidate pins changed")

    def encode(self):
        return (json.dumps(self.record, sort_keys=True, allow_nan=False) + "\n").encode()

    def sync_directory(self):
        descriptor = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def invalidate_success(self, attempted):
        """Best-effort invalidation; a failed filesystem cannot prove durability."""
        try:
            descriptor = os.open(self.path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "r+b") as stream:
                if stream.read(16385) != attempted:
                    return  # Never invalidate another writer's evidence.
                failed = dict(self.record, status="reporting_unconfirmed", ok=False,
                              exit_code=1, error="ArtifactError", durability="unknown")
                payload = (json.dumps(failed, sort_keys=True, allow_nan=False) + "\n").encode()
                # Truncate first: an interrupted/failed rewrite leaves incomplete
                # evidence rather than the previous success-shaped record.
                stream.seek(0)
                stream.truncate(0)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except (OSError, RuntimeError, ValueError):
            pass  # The original reporting failure must still fail the invocation.

    def update(self, phase, **values):
        descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            if stream.read(16385) != self.previous:
                raise ArtifactError("Run artifact changed externally")
        self.record.update(phase=phase, sequence=self.record["sequence"] + 1,
                           updated_at_unix=time.time(), **values)
        payload = self.encode()
        descriptor, temporary = tempfile.mkstemp(prefix=".gate-run-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            try:
                self.sync_directory()
            except (OSError, RuntimeError, ValueError):
                if self.record.get("ok") is True:
                    self.invalidate_success(payload)
                raise
            self.previous = payload
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


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
             min_available_bytes: int = 0, *, artifact_path: Path | None = None,
             run_id: str | None = None, expected_head: str | None = None,
             expected_tree: str | None = None) -> tuple[dict, int]:
    name = "skybuild-gate-" + secrets.token_hex(8)
    password = secrets.token_urlsafe(32)
    descriptor, log_path = tempfile.mkstemp(prefix=name + "-", suffix=".log")
    result = {"ok": False, "log": log_path, "container": name}
    exit_code = 1
    started = False
    deadline = time.monotonic() + timeout
    artifact = None
    options = (artifact_path, run_id, expected_head, expected_tree)
    if any(value is not None for value in options):
        try:
            if any(value is None for value in options):
                raise ArtifactError("All artifact options are required together")
            artifact = RunArtifact(artifact_path, checkout, run_id, expected_head, expected_tree,
                                   timeout=timeout, deadline=deadline, command=command, image=image,
                                   container=name, log=log_path, min_available_bytes=min_available_bytes)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
            os.close(descriptor)
            result.update(error="ArtifactError", exit_code=1)
            return result, 1
        result.update(run_id=run_id, artifact=str(artifact_path))
    outcome = "error"
    cleanup_status = "not_started"

    def progress(phase, **values):
        if artifact:
            try:
                artifact.update(phase, **values)
            except (OSError, RuntimeError, ValueError):
                raise ArtifactError("Progress persistence failed") from None

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
                if min_available_bytes:
                    available = available_memory_bytes()
                    if available < min_available_bytes:
                        raise RuntimeError("Available memory below gate minimum: "
                                           f"{available} bytes available; {min_available_bytes} bytes required")

            require_headroom()
            if artifact:
                artifact.check_candidate()
            progress("starting_postgres", cleanup="unknown")
            env = dict(os.environ, POSTGRES_PASSWORD=password)
            # The random loopback binding and anonymous volume belong only to this container.
            started = True
            execute(["docker", "run", "--detach", "--name", name,
                     "--label", "skybuild.disposable-gate=true", "--memory", "512m",
                     "--memory-swap", "512m", "--pids-limit", "128", "--publish", "127.0.0.1::5432",
                     "--env", "POSTGRES_PASSWORD", image], env=env)
            progress("waiting_postgres", cleanup="unknown")
            while execute(["docker", "exec", name, "pg_isready", "-h", "127.0.0.1", "-U", "postgres"], check=False).returncode:
                time.sleep(min(0.25, max(0, deadline - time.monotonic())))
            binding = execute(["docker", "port", name, "5432/tcp"], capture=True).stdout.strip()
            match = re.fullmatch(r"127\.0\.0\.1:(\d+)", binding)
            if not match:
                raise RuntimeError("Container port must bind only to localhost")
            port = int(match[1])
            progress("creating_databases")
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
            if artifact:
                artifact.check_candidate()
            progress("testing")
            exit_code = execute(command, env=test_env, check=False).returncode
            result["ok"] = exit_code == 0
            outcome = "passed" if result["ok"] else "failed"
        except (OSError, RuntimeError, subprocess.SubprocessError, TimeoutError, KeyboardInterrupt) as error:
            # Do not expose subprocess environment or database passwords in the summary.
            result["error"] = type(error).__name__
            detail = _safe_gate_diagnostic(error)
            if detail is not None:
                result["error_detail"] = detail
            outcome = ("interrupted" if isinstance(error, KeyboardInterrupt) else
                       "timed_out" if isinstance(error, (TimeoutError, subprocess.TimeoutExpired)) else "error")
            log.write(f"\nGate failed: {type(error).__name__}\n")
        finally:
            if started:
                try:
                    progress("cleaning_up", cleanup="unknown")
                except (OSError, RuntimeError, ValueError):
                    result["artifact_error"] = True
                try:
                    cleanup = subprocess.run(["docker", "rm", "--force", "--volumes", name],
                                             stdout=log, stderr=log, timeout=30, check=False)
                    result["cleaned_up"] = cleanup.returncode == 0
                except (OSError, subprocess.SubprocessError):
                    result["cleaned_up"] = False
                if not result["cleaned_up"]:
                    result["ok"] = False
                    exit_code = 1
                cleanup_status = "confirmed" if result["cleaned_up"] else "unknown"
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
    if artifact:
        try:
            try:
                artifact.check_candidate()
            except (OSError, RuntimeError, subprocess.SubprocessError):
                result.update(ok=False, exit_code=1, artifact_error=True)
                exit_code = 1
                outcome = "candidate_changed_or_unreadable"
            if result.get("artifact_error"):
                if outcome != "candidate_changed_or_unreadable":
                    raise ArtifactError("Progress persistence failed")
            artifact.update("terminal", status="cleanup_unconfirmed" if cleanup_status == "unknown" else outcome,
                            gate_outcome=outcome, cleanup=cleanup_status,
                            ok=result["ok"], exit_code=exit_code,
                            error=result.get("error"), error_detail=result.get("error_detail"))
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
            result.update(ok=False, exit_code=1, artifact_error=True)
            exit_code = 1
    return result, exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--timeout", type=float, default=600, help="Total gate deadline in seconds")
    parser.add_argument("--image", default="postgres:16", help="Disposable PostgreSQL Docker image")
    parser.add_argument("--min-available-gib", type=int, default=0,
                        help="Refuse to start PostgreSQL or tests below this available-memory threshold")
    parser.add_argument("--artifact", type=Path, help="Exclusive durable run record in an external private directory")
    parser.add_argument("--run-id")
    parser.add_argument("--expected-head")
    parser.add_argument("--expected-tree")
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
                            min_available_bytes=args.min_available_gib * 1024**3,
                            artifact_path=args.artifact, run_id=args.run_id,
                            expected_head=args.expected_head, expected_tree=args.expected_tree)
    print(json.dumps(result, sort_keys=True))
    return code if 0 <= code <= 125 else 1


if __name__ == "__main__":
    raise SystemExit(main())
