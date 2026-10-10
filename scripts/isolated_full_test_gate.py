#!/usr/bin/env python3
"""Run the unchanged SkyBuild full test command in an isolated, pinned container.

The host process is the trusted supervisor. Candidate tests run with a readonly
source archive, one scratch tmpfs, synthetic disposable database credentials,
and no host Docker socket, home directory, publishing credentials, or egress.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import resource
import secrets
import shutil
import socket
import stat
import subprocess
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from uuid import uuid4

import gate_policy


TASK_ID = "SKYBUILD-ISOLATED-CANDIDATE-FULL-TEST-GATE"
DEFAULT_GATE_COMMAND = ["uv", "run", "--extra", "test", "python", "-m", "pytest", "-q"]
DEFAULT_GATE_COMMAND_SHA256 = hashlib.sha256(json.dumps(DEFAULT_GATE_COMMAND).encode()).hexdigest()
RUNNER_VERSION = "skybuild-isolated-full-test-gate-v1"
UV_VERSION = "0.11.22"
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_HISTORY_PACK_BYTES = 64 * 1024 * 1024
HISTORY_COMMITS = [
    "6d96075f88493d0b54577a2a8c9526f19a78a5ed",
    "d79d2e1947d2c8e9edb577ab5f5093edfa3c94e3",
]
RUN_ID_LABEL = "skybuild.full-test.run-id"
KIND_LABEL = "skybuild.full-test.kind"
RUNNER_LABELS = {
    "org.skybuild.full-test.policy-sha256": "gate_policy_sha256",
    "org.skybuild.full-test.uv-lock-sha256": "uv_lock_sha256",
    "org.skybuild.full-test.pyproject-sha256": "pyproject_sha256",
    "org.skybuild.full-test.command-sha256": "gate_command_sha256",
    "org.skybuild.full-test.entrypoint-sha256": "entrypoint_sha256",
}
ENV_ALLOWLIST = [
    "HOME", "PATH", "PYTHONPATH", "PYTHONSAFEPATH", "PYTHONDONTWRITEBYTECODE",
    "PYTHONNOUSERSITE", "PYTHONUNBUFFERED", "PYTEST_ADDOPTS", "TMPDIR",
    "UV_CACHE_DIR", "UV_NO_SYNC", "UV_OFFLINE", "UV_PROJECT_ENVIRONMENT",
    "SKYBUILD_TEST_DSN", "SKYBUILD_HTTP_TEST_DSN", "SKYBUILD_IMPORT_TEST_DSN",
    "SKYBUILD_GATE_HOST_GATEWAY", "SKYBUILD_GATE_HOST_LISTENER_PORT", "SKYBUILD_GATE_RELEASE_FILE",
]
FIXTURE_ALLOWLIST = ["/runner/entrypoint.py", "/runner/network_probe.py"]
RESOURCE_LIMITS = {
    "postgres": {"memory": "1536m", "memory_swap": "1536m", "cpus": "1", "pids": 128,
                 "data_tmpfs": "1g", "log_max_size": "32m"},
    "candidate": {"memory": "4g", "memory_swap": "4g", "cpus": "2", "pids": 256,
                   "timeout_seconds": 3600, "scratch_tmpfs": "2g", "log_max_size": "128m"},
    "firewall": {"memory": "128m", "memory_swap": "128m", "cpus": "0.25", "pids": 32,
                 "log_max_size": "4m"},
    "probe": {"memory": "128m", "memory_swap": "128m", "cpus": "0.25", "pids": 32,
              "log_max_size": "4m"},
    "container_memory_total_gib": 6,
    "host_minimum_available_gib": 8,
    "host_required_available_gib": 14,
}
ATTESTED_CANDIDATE_LIMITS = {
    "cpu_millis": 2000,
    "memory_bytes": 4 * 1024**3,
    "pids": 256,
    "timeout_seconds": 3600,
}
FIREWALL_POLICY = {
    "schema": "skybuild.candidate-network-firewall.v1",
    "ipv4": {"input": "drop", "forward": "drop", "output": "drop",
             "allow_input": ["loopback", "established_related"],
             "allow_output": ["loopback", "candidate_to_exact_postgres_tcp_5432", "established_related"],
             "deny_output": ["docker_dns_address_all_ports"]},
    "ipv6": {"input": "drop", "forward": "drop", "output": "drop",
             "allow_input": ["loopback", "established_related"],
             "allow_output": ["loopback", "established_related"]},
    "postgres_namespace": {"allow_input": ["candidate_to_exact_postgres_tcp_5432"],
                            "allow_output": ["loopback", "established_related"],
                            "deny_new_outbound_ipv4": True, "deny_new_outbound_ipv6": True,
                            "allow_candidate_ingress_only": True},
    "no_host_global_rules": True,
}
FIREWALL_POLICY_SHA256 = hashlib.sha256(json.dumps(
    FIREWALL_POLICY, sort_keys=True, ensure_ascii=False, allow_nan=False,
    separators=(",", ":")).encode("utf-8")).hexdigest()
_SHA40 = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IMAGE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ACTIVE_POLICY_AUTHORIZATION: gate_policy.Authorization | None = None


class GateError(RuntimeError):
    """A qualification precondition, execution, or evidence step failed."""


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise GateError("Gate evidence is not canonical JSON") from error


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _git(checkout: Path, *args: str, timeout: int = 30) -> str:
    result = subprocess.run(["git", "-C", str(checkout), *args], capture_output=True,
                            text=True, check=False, timeout=timeout)
    if result.returncode:
        raise GateError("Git could not verify the candidate source")
    return result.stdout.strip()


def _private_regular(path: Path, *, max_bytes: int = 64 * 1024) -> bytes:
    path = Path(path)
    info = path.lstat()
    if (not path.is_absolute() or not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o077 or info.st_size > max_bytes):
        raise GateError("Input must be an owned private regular file")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (not stat.S_ISREG(opened.st_mode) or opened.st_ino != info.st_ino
                or opened.st_dev != info.st_dev):
            raise GateError("Private input changed while opening")
        data = stream.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise GateError("Private input exceeds its size limit")
    return data


def _validate_attestation_key(path: Path, key_id: str) -> None:
    info = Path(path).lstat()
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise GateError("Attestation key must be a private regular file with mode 0600")
    key = _private_regular(path, max_bytes=4096)
    if not 32 <= len(key) <= 4096 or not isinstance(key_id, str) or not key_id.strip():
        raise GateError("Attestation key material or pinned key ID is invalid")


def _read_json(path: Path, *, max_bytes: int = 64 * 1024) -> dict:
    try:
        value = json.loads(_private_regular(path, max_bytes=max_bytes))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise GateError("Private JSON input could not be read") from error
    if not isinstance(value, dict):
        raise GateError("Private JSON input must be an object")
    return value


def _require_sha(value: object, size: int = 64) -> str:
    pattern = _SHA40 if size == 40 else _SHA256
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise GateError("Expected a full Git or SHA-256 object ID")
    return value


def _read_predicate(path: Path, *, focused: bool = False) -> dict:
    predicate = _read_json(path)
    required = {
        "bundle_id", "pr_number", "target_ref", "target_base", "candidate_commit",
        "candidate_tree", "candidate_archive_sha256", "candidate_history_sha256", "gate_argv", "gate_command_sha256",
        "gate_policy_sha256", "runner_identity", "runner_version", "runner_image_id",
        "postgres_image_id", "firewall_image_id", "firewall_policy_sha256",
        "network_probe_sha256", "trusted_entrypoint_sha256", "attestation_signer_sha256",
        "candidate_blocked_until_probe",
        "execution_host",
    }
    if focused:
        required -= {"bundle_id", "pr_number"}
    if set(predicate) != required:
        raise GateError("Expected predicate fields do not match the full-test gate contract")
    if not focused and (not isinstance(predicate["bundle_id"], str) or not predicate["bundle_id"]
                        or type(predicate["pr_number"]) is not int or predicate["pr_number"] < 1):
        raise GateError("Expected full-gate bundle and PR identity is invalid")
    if (not isinstance(predicate["target_ref"], str) or not predicate["target_ref"].startswith("refs/heads/")
            or not isinstance(predicate["runner_identity"], str) or not predicate["runner_identity"]
            or predicate["runner_version"] != RUNNER_VERSION
            or predicate["execution_host"] != platform.node().split(".", 1)[0].lower()):
        raise GateError("Expected predicate identity or execution host is invalid")
    _require_sha(predicate["target_base"], 40)
    _require_sha(predicate["candidate_commit"], 40)
    _require_sha(predicate["candidate_tree"], 40)
    for key in ("candidate_archive_sha256", "candidate_history_sha256", "gate_command_sha256", "gate_policy_sha256",
                "firewall_policy_sha256", "network_probe_sha256", "trusted_entrypoint_sha256",
                "attestation_signer_sha256"):
        _require_sha(predicate[key])
    if predicate["gate_argv"] != DEFAULT_GATE_COMMAND or predicate["gate_command_sha256"] != DEFAULT_GATE_COMMAND_SHA256:
        raise GateError("Frozen gate command differs from the unchanged full-test policy")
    if any(not _IMAGE.fullmatch(predicate[key]) for key in
           ("runner_image_id", "postgres_image_id", "firewall_image_id")):
        raise GateError("Expected predicate must pin all immutable local images")
    if predicate["firewall_policy_sha256"] != FIREWALL_POLICY_SHA256:
        raise GateError("Firewall policy digest differs from the reviewed IPv4/IPv6 default-deny policy")
    if predicate["candidate_blocked_until_probe"] is not True:
        raise GateError("Candidate must remain blocked until trusted network probes pass")
    return predicate


def _bounded_git_output(checkout: Path, args: list[str], destination: Path,
                        max_bytes: int, *, input_bytes: bytes | None = None) -> tuple[str, int]:
    """Cap producer writes in the kernel, including before the parent can observe them."""
    def cap_output() -> None:
        resource.setrlimit(resource.RLIMIT_FSIZE, (max_bytes, max_bytes))

    with destination.open("xb") as output:
        process = subprocess.Popen(["git", "-C", str(checkout), *args], stdout=output,
                                   stderr=subprocess.DEVNULL,
                                   stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
                                   preexec_fn=cap_output)
        try:
            process.communicate(input=input_bytes, timeout=120)
        except subprocess.TimeoutExpired as error:
            process.kill()
            process.communicate(timeout=5)
            raise GateError("Bounded Git producer exceeded its deadline") from error
        output.flush()
        os.fsync(output.fileno())
    if process.returncode:
        raise GateError("Bounded Git producer failed or exceeded its byte limit")
    size = destination.stat().st_size
    if size < 1 or size >= max_bytes:
        raise GateError("Git output size is outside the reviewed limit")
    digest = hashlib.sha256()
    with destination.open("rb") as archive:
        for chunk in iter(lambda: archive.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest(), size


def _archive(checkout: Path, commit: str, destination: Path) -> tuple[str, int]:
    return _bounded_git_output(checkout, ["archive", "--format=tar", commit],
                               destination, MAX_ARCHIVE_BYTES)


def _history_fixture(checkout: Path, destination: Path) -> tuple[str, int]:
    """Construct only pinned source objects; never copy operational Git metadata."""
    destination.mkdir(mode=0o755)
    destination.chmod(0o755)
    objects = destination / "objects"
    packs = objects / "pack"
    packs.mkdir(parents=True, mode=0o755)
    listing = destination / "object-list"
    _bounded_git_output(checkout, ["rev-list", "--objects", "--no-walk=unsorted", *HISTORY_COMMITS],
                        listing, 4 * 1024 * 1024)
    object_ids = []
    for line in listing.read_bytes().splitlines():
        object_id = line.split(b" ", 1)[0].decode("ascii")
        _require_sha(object_id, 40)
        object_ids.append(object_id)
    if not object_ids or len(object_ids) > 100_000:
        raise GateError("History fixture object count is outside its limit")
    pack_path = packs / "pack-fixture.pack"
    _, pack_size = _bounded_git_output(checkout, [
        "pack-objects", "--stdout", "--no-reuse-delta", "--no-reuse-object",
        "--threads=1", "--compression=9", "--window=0",
    ], pack_path, MAX_HISTORY_PACK_BYTES, input_bytes=("\n".join(object_ids) + "\n").encode())
    listing.unlink()
    _git(checkout, "index-pack", str(pack_path), timeout=120)
    refs = destination / "refs/heads"
    refs.mkdir(parents=True, mode=0o755)
    for index, commit in enumerate(HISTORY_COMMITS):
        (refs / ("fixture-" + str(index))).write_text(commit + "\n", encoding="ascii")
    (destination / "HEAD").write_text("ref: refs/heads/fixture-0\n", encoding="ascii")
    (destination / "shallow").write_text("\n".join(HISTORY_COMMITS) + "\n", encoding="ascii")
    (destination / "config").write_text("[core]\n\trepositoryformatversion = 0\n\tbare = false\n", encoding="ascii")
    # Include both pack and index plus the entire synthetic metadata in this digest.
    manifest = {}
    for path in sorted(destination.rglob("*")):
        if path.is_file():
            path.chmod(0o444)
            manifest[path.relative_to(destination).as_posix()] = _digest_bytes(path.read_bytes())
        elif path.is_dir():
            path.chmod(0o755)
    return _digest_bytes(_canonical(manifest)), pack_size


def _extract_archive(archive: Path, destination: Path) -> None:
    destination.mkdir(mode=0o700)
    try:
        with tarfile.open(archive, mode="r:") as tar:
            members = tar.getmembers()
            if not members or len(members) > 100_000:
                raise GateError("Candidate archive member count is outside the reviewed limit")
            seen: set[str] = set()
            for member in members:
                name = PurePosixPath(member.name)
                normalized = name.as_posix()
                if (name.is_absolute() or ".." in name.parts or not name.parts
                        or name.parts[0] == ".git"
                        or normalized in seen
                        or not (member.isfile() or member.isdir())):
                    raise GateError("Candidate archive contains an unsafe member")
                seen.add(normalized)
            tar.extractall(destination, filter="data")
    except (OSError, tarfile.TarError, ValueError) as error:
        raise GateError("Candidate archive extraction failed safely") from error
    if not (destination / "src/skybuild/__init__.py").is_file() or not (destination / "tests").is_dir():
        raise GateError("Candidate archive is missing the expected SkyBuild source or tests")


def _candidate_identity(checkout: Path, predicate: dict) -> dict:
    checkout = checkout.resolve(strict=True)
    if _git(checkout, "rev-parse", "--show-toplevel") != str(checkout):
        raise GateError("Checkout must name its exact Git root")
    origin = _git(checkout, "remote", "get-url", "origin")
    if origin.rstrip("/").removesuffix(".git") != "https://github.com/stonesky-ai/skybuild":
        raise GateError("Checkout origin must identify SkyBuild")
    if _git(checkout, "status", "--porcelain", "--untracked-files=all"):
        raise GateError("Candidate checkout must be clean")
    head = _git(checkout, "rev-parse", "HEAD")
    tree = _git(checkout, "rev-parse", "HEAD^{tree}")
    if head != predicate["candidate_commit"] or tree != predicate["candidate_tree"]:
        raise GateError("Checkout does not match frozen candidate commit/tree")
    ancestry = subprocess.run(
        ["git", "-C", str(checkout), "merge-base", "--is-ancestor",
         predicate["target_base"], head], capture_output=True, check=False, timeout=30,
    )
    if ancestry.returncode != 0:
        raise GateError("Candidate commit is not descended from the frozen target base")
    return {"checkout": str(checkout), "commit": head, "tree": tree}


def _runner_provenance() -> dict:
    runner_root = Path(__file__).resolve().parents[1]
    if _git(runner_root, "rev-parse", "--show-toplevel") != str(runner_root):
        raise GateError("Trusted runner must execute from its exact Git root")
    if _git(runner_root, "status", "--porcelain", "--untracked-files=all"):
        raise GateError("Trusted runner checkout must be clean before review and execution")
    script = Path(__file__).resolve()
    entrypoint = script.with_name("gate_container_entrypoint.py")
    probe = script.with_name("gate_network_probe.py")
    signer = script.with_name("trusted_gate_attestation.py")
    if not signer.is_file():
        raise GateError("Trusted attestation signer source is missing from the frozen runner checkout")
    return {
        "commit": _require_sha(_git(runner_root, "rev-parse", "HEAD"), 40),
        "tree": _require_sha(_git(runner_root, "rev-parse", "HEAD^{tree}"), 40),
        "script_sha256": _digest_bytes(script.read_bytes()),
        "entrypoint_sha256": _digest_bytes(entrypoint.read_bytes()),
        "network_probe_sha256": _digest_bytes(probe.read_bytes()),
        "attestation_signer_sha256": _digest_bytes(signer.read_bytes()),
        "policy_module_sha256": _digest_bytes(script.with_name("gate_policy.py").read_bytes()),
    }


def prepare(checkout: Path, predicate_path: Path, runner_image_id: str | None = None,
            postgres_image_id: str | None = None, *, _focused: bool = False) -> dict:
    predicate = _read_predicate(predicate_path, focused=_focused)
    if ((runner_image_id is not None and runner_image_id != predicate["runner_image_id"])
            or (postgres_image_id is not None and postgres_image_id != predicate["postgres_image_id"])):
        raise GateError("CLI image pins differ from frozen predicate")
    candidate = _candidate_identity(checkout, predicate)
    with tempfile.TemporaryDirectory(prefix="skybuild-gate-archive-") as scratch:
        archive = Path(scratch) / "candidate.tar"
        archive_sha256, archive_size = _archive(Path(candidate["checkout"]), candidate["commit"], archive)
        history_sha256, history_size = _history_fixture(Path(candidate["checkout"]), Path(scratch) / "history")
    if archive_sha256 != predicate["candidate_archive_sha256"]:
        raise GateError("Candidate archive SHA-256 differs from the frozen predicate")
    if history_sha256 != predicate["candidate_history_sha256"]:
        raise GateError("Sanitized history fixture SHA-256 differs from the frozen predicate")
    actual_entrypoint = _digest_bytes(Path(__file__).with_name("gate_container_entrypoint.py").read_bytes())
    actual_probe = _digest_bytes(Path(__file__).with_name("gate_network_probe.py").read_bytes())
    if actual_entrypoint != predicate["trusted_entrypoint_sha256"]:
        raise GateError("Trusted entrypoint SHA-256 differs from the frozen predicate")
    if actual_probe != predicate["network_probe_sha256"]:
        raise GateError("Trusted network probe SHA-256 differs from the frozen predicate")
    runner = _runner_provenance()
    if runner["attestation_signer_sha256"] != predicate["attestation_signer_sha256"]:
        raise GateError("Trusted attestation signer SHA-256 differs from the frozen predicate")
    return {
        "task_id": TASK_ID,
        "execution_host": predicate["execution_host"],
        "runner_source": runner,
        "candidate": {**candidate, "archive_sha256": archive_sha256, "archive_size": archive_size,
                      "history_sha256": history_sha256, "history_pack_size": history_size},
        "gate": {"argv": DEFAULT_GATE_COMMAND, "command_sha256": DEFAULT_GATE_COMMAND_SHA256,
                 "policy_sha256": predicate["gate_policy_sha256"]},
        "images": {"runner_image_id": predicate["runner_image_id"],
                   "postgres_image_id": predicate["postgres_image_id"],
                   "firewall_image_id": predicate["firewall_image_id"]},
        "policy": {"predicate_sha256": _digest_bytes(_canonical(predicate)),
                   "environment_allowlist": ENV_ALLOWLIST,
                   "readonly_fixture_allowlist": FIXTURE_ALLOWLIST,
                   "firewall_policy_sha256": predicate["firewall_policy_sha256"],
                   "history_fixture": {"commits": HISTORY_COMMITS,
                                       "max_pack_bytes": MAX_HISTORY_PACK_BYTES, "readonly": True},
                   "resource_limits": RESOURCE_LIMITS,
                   "mounts": [
                       {"target": "/candidate", "mode": "ro", "kind": "bind",
                        "source_class": "candidate_archive", "source": "sha256:" + archive_sha256},
                   {"target": "/scratch", "mode": "rw", "kind": "tmpfs",
                        "source_class": "scratch", "source": "tmpfs"},
                       {"target": "/runner/entrypoint.py", "mode": "ro", "kind": "bind",
                        "source_class": "trusted_runner_fixture",
                        "source": "sha256:" + predicate["trusted_entrypoint_sha256"]},
                       {"target": "/runner/network_probe.py", "mode": "ro", "kind": "bind",
                        "source_class": "trusted_runner_fixture",
                        "source": "sha256:" + predicate["network_probe_sha256"]},
                   ]},
        "runtime_plan": [
            "Verify exact immutable local runner/PostgreSQL/firewall image IDs, source digests, and dependency labels before starting resources.",
            "Create one labeled internal-only IPv4 network, bounded PostgreSQL tmpfs/container, and capped test container.",
            "Run candidate source from a readonly archive mount; trusted entrypoint copies it into one tmpfs scratch mount.",
            "Hold candidate at a trusted entrypoint barrier; install and inspect default-deny IPv4/IPv6 firewall sidecars in both candidate and PostgreSQL namespaces.",
            "Run trusted pre-release probes for database access, DNS, external IPv4/IPv6, and an owned random-port host listener in both namespaces; release candidate only after success.",
            "Pass only allowlisted synthetic DSNs and fixed runtime environment; expose no host mounts, secrets, home, socket, or egress.",
            "Supervisor records exact container exit status and redacted log hash, then reconciles every owned container/network by full ID and labels.",
            "Sign the result only from the trusted host after the actual run; preserve unconfirmed cleanup as a failed receipt.",
        ],
    }


def plan_digest(plan: dict) -> str:
    return _digest_bytes(_canonical(plan))


class Journal:
    def __init__(self, path: Path, metadata: dict):
        self.path = path
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        self.stream = os.fdopen(descriptor, "w", encoding="utf-8")
        self.sequence = 0
        self.event("run_started", **metadata)

    def event(self, name: str, **fields) -> None:
        row = {"sequence": self.sequence, "event": name, "at_utc": _now(), **fields}
        payload = json.dumps(row, sort_keys=True, allow_nan=False) + "\n"
        self.stream.write(payload)
        self.stream.flush()
        os.fsync(self.stream.fileno())
        self.sequence += 1

    def close(self) -> None:
        self.stream.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _docker(*args: str, timeout: int = 30, input_text: str | None = None,
            check: bool = True) -> subprocess.CompletedProcess[str]:
    if _ACTIVE_POLICY_AUTHORIZATION is not None:
        _ACTIVE_POLICY_AUTHORIZATION.check()
    try:
        result = subprocess.run(["docker", *args], input=input_text, capture_output=True,
                                text=True, check=False, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as error:
        raise GateError("Docker command could not be completed: " + (args[0] if args else "unknown")) from error
    if check and result.returncode:
        raise GateError("Docker command failed: " + (args[0] if args else "unknown"))
    return result


def _inspect_image(image_id: str) -> dict:
    result = _docker("image", "inspect", image_id, timeout=20)
    try:
        rows = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise GateError("Pinned test image metadata is invalid") from error
    if len(rows) != 1 or rows[0].get("Id") != image_id:
        raise GateError("Pinned test image ID changed")
    return rows[0]


def _validate_runner_image(image: dict, predicate: dict, archive_root: Path,
                           entrypoint_sha256: str) -> dict[str, str]:
    labels = image.get("Config", {}).get("Labels") or {}
    expected = {
        "org.skybuild.full-test.policy-sha256": predicate["gate_policy_sha256"],
        "org.skybuild.full-test.uv-lock-sha256": _digest_bytes((archive_root / "uv.lock").read_bytes()),
        "org.skybuild.full-test.pyproject-sha256": _digest_bytes((archive_root / "pyproject.toml").read_bytes()),
        "org.skybuild.full-test.command-sha256": DEFAULT_GATE_COMMAND_SHA256,
        "org.skybuild.full-test.entrypoint-sha256": entrypoint_sha256,
        "org.skybuild.full-test.network-probe-sha256": predicate["network_probe_sha256"],
        "org.skybuild.full-test.uv-version": UV_VERSION,
    }
    if any(labels.get(key) != value for key, value in expected.items()):
        raise GateError("Pinned runner image labels do not match frozen gate policy/dependencies/entrypoint")
    if not _IMAGE.fullmatch(image.get("Id", "")):
        raise GateError("Pinned runner image has no immutable ID")
    image_env = image.get("Config", {}).get("Env") or []
    names = {entry.split("=", 1)[0] for entry in image_env if isinstance(entry, str) and "=" in entry}
    if not names <= set(ENV_ALLOWLIST) or "PATH" not in names:
        raise GateError("Pinned runner image contains unexpected default environment variables")
    return {entry.split("=", 1)[0]: entry.split("=", 1)[1] for entry in image_env}


def _validate_firewall_image(image: dict, predicate: dict) -> None:
    if image.get("Id") != predicate["firewall_image_id"]:
        raise GateError("Pinned firewall image ID changed")
    labels = image.get("Config", {}).get("Labels") or {}
    if labels.get("org.skybuild.full-test.firewall-policy-sha256") != FIREWALL_POLICY_SHA256:
        raise GateError("Firewall image does not bind the reviewed policy digest")


def _available_gib() -> float:
    try:
        text = Path("/proc/meminfo").read_text(encoding="ascii")
        match = re.search(r"^MemAvailable:\s+(\d+) kB$", text, re.MULTILINE)
    except OSError as error:
        raise GateError("Host available memory cannot be measured") from error
    if not match:
        raise GateError("Host available memory cannot be measured")
    available_bytes = int(match[1]) * 1024
    cgroup_pairs = (
        (Path("/sys/fs/cgroup/memory.max"), Path("/sys/fs/cgroup/memory.current")),
        (Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
         Path("/sys/fs/cgroup/memory/memory.usage_in_bytes")),
    )
    for limit_path, current_path in cgroup_pairs:
        if not limit_path.exists() and not current_path.exists():
            continue
        try:
            limit_text = limit_path.read_text(encoding="ascii").strip()
            current = int(current_path.read_text(encoding="ascii").strip())
            if limit_text != "max":
                limit = int(limit_text)
                if 0 < limit < (1 << 60):
                    available_bytes = min(available_bytes, max(0, limit - current))
        except (OSError, ValueError) as error:
            raise GateError("Host cgroup memory headroom cannot be measured") from error
        break
    return available_bytes / (1024**3)


def _ensure_private_output(output_dir: Path) -> Path:
    output_dir = Path(output_dir).resolve(strict=True)
    info = output_dir.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077):
        raise GateError("Output directory must be owned and private mode 0700")
    return output_dir


def _dsn(password: str, database: str) -> str:
    return f"postgresql://postgres:{password}@db:5432/{database}"


def _candidate_environment(password: str) -> dict[str, str]:
    scratch = "/scratch"
    return {
        "HOME": scratch + "/home", "PATH": "/usr/local/bin:/usr/bin:/bin",
        "PYTHONPATH": "/scratch/workspace/src:/scratch/workspace/scripts:/scratch/workspace",
        "PYTHONSAFEPATH": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1", "PYTHONUNBUFFERED": "1",
        "PYTEST_ADDOPTS": "--basetemp=/scratch/pytest-tmp -o cache_dir=/scratch/pytest-cache",
        "TMPDIR": scratch + "/tmp", "UV_CACHE_DIR": scratch + "/uv-cache",
        "UV_NO_SYNC": "1", "UV_OFFLINE": "1", "UV_PROJECT_ENVIRONMENT": "/scratch/workspace/.venv",
        "SKYBUILD_TEST_DSN": _dsn(password, "skybuild_test"),
        "SKYBUILD_HTTP_TEST_DSN": _dsn(password, "skybuild_http_test"),
        "SKYBUILD_IMPORT_TEST_DSN": _dsn(password, "skybuild_import_test"),
        "SKYBUILD_GATE_HOST_GATEWAY": "",
        "SKYBUILD_GATE_HOST_LISTENER_PORT": "",
        "SKYBUILD_GATE_RELEASE_FILE": "/scratch/.firewall-ready",
    }


def _trusted_probe_environment() -> dict[str, str]:
    # Override every allowed image variable so probes receive no image-baked
    # DSNs, credentials, or host configuration.
    environment = {name: "" for name in ENV_ALLOWLIST}
    environment.update({
        "HOME": "/tmp", "PATH": "/usr/local/bin:/usr/bin:/bin",
        "PYTHONNOUSERSITE": "1", "PYTHONSAFEPATH": "1",
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1",
    })
    return environment


def _firewall_script(postgres_ip: str, candidate_ip: str, *, postgres_namespace: bool) -> str:
    # The PG address is parsed as IPv4 before interpolation. The candidate has
    # no NET_ADMIN capability and cannot change these rules.
    input_rules = ("iptables -w -A INPUT -s " + candidate_ip
                   + "/32 -p tcp --dport 5432 -j ACCEPT",) if postgres_namespace else ()
    output_rules = (
        "iptables -w -A OUTPUT -d 127.0.0.11/32 -j DROP",
    )
    if not postgres_namespace:
        output_rules += ("iptables -w -A OUTPUT -d " + postgres_ip + "/32 -p tcp --dport 5432 -j ACCEPT",)
    return "\n".join((
        "set -eu",
        "iptables -w -F INPUT; iptables -w -F OUTPUT; iptables -w -F FORWARD",
        "iptables -w -P INPUT DROP; iptables -w -P OUTPUT DROP; iptables -w -P FORWARD DROP",
        "iptables -w -A INPUT -i lo -j ACCEPT",
        *input_rules,
        "iptables -w -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT",
        *output_rules,
        "iptables -w -A OUTPUT -o lo -j ACCEPT",
        "iptables -w -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT",
        "ip6tables -w -F INPUT; ip6tables -w -F OUTPUT; ip6tables -w -F FORWARD",
        "ip6tables -w -P INPUT DROP; ip6tables -w -P OUTPUT DROP; ip6tables -w -P FORWARD DROP",
        "ip6tables -w -A INPUT -i lo -j ACCEPT",
        "ip6tables -w -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT",
        "ip6tables -w -A OUTPUT -o lo -j ACCEPT",
        "ip6tables -w -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT",
        "echo SKYBUILD_FIREWALL_POLICY_APPLIED",
        "exec sleep 3700",
    ))


def _saved_rules(value: str) -> list[str]:
    rows = []
    tables = 0
    committed = 0
    for line in value.splitlines():
        if not line:
            continue
        if re.fullmatch(r"# (Generated by iptables-save v.+ on .+|Completed on .+)", line):
            continue
        if line == "*filter":
            tables += 1
        elif line == "COMMIT":
            committed += 1
        elif line.startswith(":"):
            match = re.fullmatch(r":(INPUT|FORWARD|OUTPUT) (ACCEPT|DROP) \[\d+:\d+\]", line)
            if not match:
                raise GateError("Firewall has an unexpected table or user-defined chain")
            rows.append(match.group(1) + " " + match.group(2))
        elif line.startswith("-A "):
            rows.append(line)
        else:
            raise GateError("Firewall save output has an unexpected table or rule directive")
    if tables != 1 or committed != 1:
        raise GateError("Firewall save output must contain only the inspected filter table")
    return rows


def _expected_firewall_rules(postgres_ip: str, candidate_ip: str, *, ipv6: bool,
                             postgres_namespace: bool) -> list[str]:
    prefix = "-A "
    rules = ["INPUT DROP", "FORWARD DROP", "OUTPUT DROP"]
    rules.extend((
        prefix + "INPUT -i lo -j ACCEPT",
    ))
    if postgres_namespace and not ipv6:
        rules.append(prefix + "INPUT -s " + candidate_ip + "/32 -p tcp -m tcp --dport 5432 -j ACCEPT")
    rules.append(prefix + "INPUT -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT")
    if not ipv6:
        rules.extend((
            prefix + "OUTPUT -d 127.0.0.11/32 -j DROP",
        ))
        if not postgres_namespace:
            rules.append(prefix + "OUTPUT -d " + postgres_ip + "/32 -p tcp -m tcp --dport 5432 -j ACCEPT")
    rules.extend((
        prefix + "OUTPUT -o lo -j ACCEPT",
        prefix + "OUTPUT -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT",
    ))
    return rules


def _verify_firewall_rules(container_id: str, postgres_ip: str, candidate_ip: str,
                           *, postgres_namespace: bool) -> dict[str, bool]:
    v4 = _docker("exec", container_id, "iptables-save", "-t", "filter", timeout=10)
    v6 = _docker("exec", container_id, "ip6tables-save", "-t", "filter", timeout=10)
    v4_expected = _expected_firewall_rules(postgres_ip, candidate_ip, ipv6=False,
                                           postgres_namespace=postgres_namespace)
    v6_expected = _expected_firewall_rules(postgres_ip, candidate_ip, ipv6=True,
                                           postgres_namespace=postgres_namespace)
    ipv4_default = _saved_rules(v4.stdout)[:3] == v4_expected[:3]
    ipv6_default = _saved_rules(v6.stdout)[:3] == v6_expected[:3]
    applied = (_saved_rules(v4.stdout) == v4_expected and _saved_rules(v6.stdout) == v6_expected)
    if not ipv4_default or not ipv6_default or not applied:
        raise GateError("Firewall sidecar rules differ from exact dual-stack default-deny policy")
    return {"firewall_defaults_drop": True, "firewall_ipv4_default_drop": True,
            "firewall_ipv6_default_drop": True, "firewall_policy_applied": True}


class _OwnedHostListener:
    def __init__(self, address: str):
        listener = None
        try:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            listener.bind((address, 0))
            listener.listen(4)
            self.port = listener.getsockname()[1]
            if not 1024 <= self.port <= 65535 or self.port == 5432:
                raise GateError("Owned host listener did not receive a valid ephemeral port")
            self.socket = listener
        except OSError as error:
            if listener is not None:
                listener.close()
            raise GateError("Owned unexpected-port host listener could not be bound") from error
        except Exception:
            if listener is not None:
                listener.close()
            raise

    def close(self) -> None:
        self.socket.close()


def _verify_container(row: dict, *, name: str, run_id: str, expected_id: str | None,
                      kind: str, image_id: str | None = None) -> str:
    container_id = row.get("Id", "")
    labels = row.get("Config", {}).get("Labels") or {}
    if (not re.fullmatch(r"[0-9a-f]{64}", container_id)
            or (expected_id is not None and container_id != expected_id)
            or row.get("Name", "").lstrip("/") != name
            or labels.get(RUN_ID_LABEL) != run_id or labels.get(KIND_LABEL) != kind
            or (image_id is not None and row.get("Image") != image_id)):
        raise GateError("Container identity or ownership label differs from the run journal")
    return container_id


def _container_ipv4(row: dict, network: str) -> str:
    try:
        address = ipaddress.ip_address(row["NetworkSettings"]["Networks"][network]["IPAddress"])
    except (KeyError, ValueError, TypeError) as error:
        raise GateError("Owned container has no valid address on the isolated network") from error
    if address.version != 4 or address.is_unspecified or address.is_multicast:
        raise GateError("Expected a concrete isolated-network IPv4 address")
    return str(address)


def _wait_candidate_blocked(name: str, run_id: str, container_id: str,
                            image_id: str, journal: Journal) -> None:
    marker = "GATE_CANDIDATE_BLOCKED=firewall_release_pending"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        row = _container_info(name, run_id, container_id, "candidate", image_id)
        if row is None or row.get("State", {}).get("Status") == "exited":
            raise GateError("Candidate entrypoint exited before firewall release")
        result = _docker("logs", container_id, check=False, timeout=10)
        if result.returncode:
            raise GateError("Candidate blocked-state logs could not be read")
        if marker in result.stdout:
            forbidden = ("GATE_CANDIDATE_ARCHIVE_READONLY", "GATE_CANDIDATE_COPY",
                         "GATE_PREFLIGHT_IMPORT", "pytest")
            if any(value in result.stdout for value in forbidden):
                raise GateError("Candidate source was processed before trusted probe release")
            journal.event("candidate_blocked_before_probe", container_id=container_id,
                          marker=marker)
            return
        time.sleep(0.25)
    raise GateError("Candidate did not acknowledge the firewall-release barrier")


def _wait_exit(name: str, run_id: str, container_id: str, kind: str,
               image_id: str, deadline: float) -> int:
    while time.monotonic() < deadline:
        row = _container_info(name, run_id, container_id, kind, image_id)
        if row is None:
            raise GateError("Owned container disappeared before exit status was observed")
        state = row.get("State", {})
        if state.get("Status") == "exited":
            code = state.get("ExitCode")
            if type(code) is int and code >= 0:
                return code
            raise GateError("Owned container exit status is malformed")
        time.sleep(0.2)
    raise GateError("Owned container exceeded its bounded execution deadline")


def _missing_container_error(stderr: str, name: str) -> bool:
    return stderr.strip() in {
        f"Error: No such object: {name}",
        f"Error: No such container: {name}",
        f"Error response from daemon: No such object: {name}",
        f"Error response from daemon: No such container: {name}",
        f"Error response from daemon: no such object: {name}",
        f"Error response from daemon: no such container: {name}",
    }


def _missing_network_error(stderr: str, name: str) -> bool:
    return stderr.strip() in {
        f"Error: No such network: {name}",
        f"Error response from daemon: No such network: {name}",
        f"Error response from daemon: no such network: {name}",
        f"Error response from daemon: network {name} not found",
    }


def _container_info(name: str, run_id: str, expected_id: str | None, kind: str,
                    image_id: str | None = None) -> dict | None:
    target = expected_id or name
    result = _docker("inspect", "--type", "container", target, check=False)
    if result.returncode:
        if _missing_container_error(result.stderr, target):
            if expected_id is None:
                return None
            by_name = _docker("inspect", "--type", "container", name, check=False)
            if by_name.returncode and _missing_container_error(by_name.stderr, name):
                return None
            if by_name.returncode:
                raise GateError("Container absence/name reconciliation is unknown")
            rows = json.loads(by_name.stdout)
        else:
            if expected_id is None:
                raise GateError("Container creation outcome cannot be reconciled")
            by_name = _docker("inspect", "--type", "container", name, check=False)
            if by_name.returncode:
                raise GateError("Container ID inspection failed and name reconciliation is uncertain")
            rows = json.loads(by_name.stdout)
    else:
        rows = json.loads(result.stdout)
    if len(rows) != 1:
        raise GateError("Container inspection returned an ambiguous identity")
    _verify_container(rows[0], name=name, run_id=run_id, expected_id=expected_id,
                      kind=kind, image_id=image_id)
    return rows[0]


def _container_absent(name: str, run_id: str, container_id: str | None, kind: str) -> bool:
    row = _container_info(name, run_id, container_id, kind)
    if row is not None:
        return False
    return True


def _remove_container(name: str, run_id: str, container_id: str | None,
                      kind: str, journal: Journal) -> bool:
    row = _container_info(name, run_id, container_id, kind)
    if row is None:
        journal.event("container_cleanup_confirmed_absent", container=name, container_id=container_id)
        return True
    owned_id = row["Id"]
    try:
        if row.get("State", {}).get("Running"):
            _docker("stop", "--time", "10", owned_id, timeout=20)
        _docker("rm", "--force", "--volumes", owned_id, timeout=30)
    except GateError:
        # A timeout can race successful removal. Verify by immutable ID and unique name.
        pass
    if not _container_absent(name, run_id, owned_id, kind):
        raise GateError("Owned container removal was not confirmed")
    journal.event("container_cleanup_confirmed_absent", container=name, container_id=owned_id)
    return True


def _network_info(name: str, run_id: str, expected_id: str | None) -> dict | None:
    result = _docker("network", "inspect", name, check=False)
    if result.returncode:
        if _missing_network_error(result.stderr, name):
            return None
        raise GateError("Network inspection outcome is unknown")
    rows = json.loads(result.stdout)
    if len(rows) != 1:
        raise GateError("Network inspection returned an ambiguous identity")
    row = rows[0]
    labels = row.get("Labels") or {}
    if (not re.fullmatch(r"[0-9a-f]{64}", row.get("Id", ""))
            or (expected_id is not None and row["Id"] != expected_id)
            or labels.get(RUN_ID_LABEL) != run_id or row.get("Internal") is not True
            or row.get("EnableIPv6") is not False):
        raise GateError("Network identity, ownership, or internal setting differs")
    return row


def _remove_network(name: str, run_id: str, network_id: str | None, journal: Journal) -> bool:
    row = _network_info(name, run_id, network_id)
    if row is None:
        journal.event("network_cleanup_confirmed_absent", network=name, network_id=network_id)
        return True
    try:
        _docker("network", "rm", row["Id"], timeout=20)
    except GateError:
        pass
    if _network_info(name, run_id, row["Id"]) is not None:
        raise GateError("Owned internal network removal was not confirmed")
    journal.event("network_cleanup_confirmed_absent", network=name, network_id=row["Id"])
    return True


def _candidate_mounts(archive_root: Path, fixture_path: Path, probe_path: Path,
                      archive_sha256: str, fixture_sha256: str,
                      probe_sha256: str) -> list[dict]:
    return [
        {"target": "/candidate", "mode": "ro", "kind": "bind", "source_class": "candidate_archive",
         "source": str(archive_root)},
        {"target": "/scratch", "mode": "rw", "kind": "tmpfs", "source_class": "scratch",
         "source": "tmpfs"},
        {"target": "/runner/entrypoint.py", "mode": "ro", "kind": "bind",
         "source_class": "trusted_runner_fixture", "source": str(fixture_path)},
        {"target": "/runner/network_probe.py", "mode": "ro", "kind": "bind",
         "source_class": "trusted_runner_fixture", "source": str(probe_path)},
    ]


def _check_candidate_inspect(row: dict, *, name: str, run_id: str, container_id: str,
                             image_id: str, archive_root: Path, fixture_path: Path,
                             probe_path: Path,
                             env: dict[str, str], network: str,
                             postgres_ip: str,
                             archive_sha256: str, fixture_sha256: str,
                             probe_sha256: str, command: list[str] | None = None) -> dict:
    _verify_container(row, name=name, run_id=run_id, expected_id=container_id,
                      kind="candidate", image_id=image_id)
    config = row.get("Config", {})
    host = row.get("HostConfig", {})
    log_config = host.get("LogConfig", {})
    expected_command = DEFAULT_GATE_COMMAND if command is None else command
    if (expected_command not in [DEFAULT_GATE_COMMAND, *gate_policy.FOCUSED_COMMANDS.values()]
            or config.get("Cmd") != expected_command or config.get("User") != "10001:10001"):
        raise GateError("Candidate command or unprivileged container user differs")
    actual_env = {}
    for entry in config.get("Env", []):
        if not isinstance(entry, str) or "=" not in entry:
            raise GateError("Candidate environment is malformed")
        key, value = entry.split("=", 1)
        if key in actual_env:
            raise GateError("Candidate environment contains duplicate names")
        actual_env[key] = value
    if actual_env != env or sorted(actual_env) != sorted(ENV_ALLOWLIST):
        raise GateError("Candidate environment is not the exact allowlist")
    actual_mounts = []
    for mount in row.get("Mounts", []):
        destination = mount.get("Destination")
        if destination == "/candidate":
            actual_mounts.append({"target": destination, "mode": "ro" if mount.get("RW") is False else "rw",
                                  "kind": mount.get("Type"), "source_class": "candidate_archive",
                                  "source": mount.get("Source")})
        elif destination == "/scratch":
            actual_mounts.append({"target": destination, "mode": "rw" if mount.get("RW") is True else "ro",
                                  "kind": mount.get("Type"), "source_class": "scratch",
                                  "source": "tmpfs"})
        elif destination == "/runner/entrypoint.py":
            actual_mounts.append({"target": destination, "mode": "ro" if mount.get("RW") is False else "rw",
                                  "kind": mount.get("Type"), "source_class": "trusted_runner_fixture",
                                  "source": mount.get("Source")})
        elif destination == "/runner/network_probe.py":
            actual_mounts.append({"target": destination, "mode": "ro" if mount.get("RW") is False else "rw",
                                  "kind": mount.get("Type"), "source_class": "trusted_runner_fixture",
                                  "source": mount.get("Source")})
        else:
            raise GateError("Candidate has an unapproved mount")
    if sorted(actual_mounts, key=lambda row: row["target"]) != sorted(
            _candidate_mounts(archive_root, fixture_path, probe_path, archive_sha256,
                              fixture_sha256, probe_sha256),
            key=lambda row: row["target"]):
        raise GateError("Candidate mounts differ from the readonly archive, scratch, and fixture allowlist")
    if (config.get("Entrypoint") != ["/runner/entrypoint.py"]
            or host.get("ReadonlyRootfs") is not True or host.get("NetworkMode") != network
            or host.get("Memory") != 4 * 1024**3 or host.get("MemorySwap") != 4 * 1024**3
            or host.get("NanoCpus") != 2_000_000_000 or host.get("PidsLimit") != 256
            or host.get("ShmSize") != 256 * 1024**2 or host.get("Privileged") is not False
            or log_config.get("Type") != "local"
            or log_config.get("Config") != {"max-size": "128m", "max-file": "2"}
            or host.get("PidMode") == "host" or host.get("IpcMode") == "host"
            or host.get("PortBindings") not in ({}, None)
            or host.get("CapAdd") not in ([], None)
            or "ALL" not in host.get("CapDrop", [])
            or "no-new-privileges:true" not in host.get("SecurityOpt", [])
            or host.get("Tmpfs") is None
            or set(host["Tmpfs"]) != {"/scratch"}
            or not {"size=2147483648", "uid=10001", "gid=10001", "mode=448"}
            <= set(host["Tmpfs"]["/scratch"].split(","))
            or not {"rw", "exec", "nosuid", "nodev"} <= set(host["Tmpfs"]["/scratch"].split(","))
            or "noexec" in host["Tmpfs"]["/scratch"].split(",")
            or host.get("ExtraHosts") != ["db:" + postgres_ip]):
        raise GateError("Candidate container lacks reviewed isolation/resource settings")
    networks = (row.get("NetworkSettings", {}).get("Networks") or {})
    if set(networks) != {network}:
        raise GateError("Candidate is attached to an unapproved Docker network")
    receipt_mounts = [dict(mount) for mount in actual_mounts]
    for mount in receipt_mounts:
        if mount["target"] == "/candidate":
            mount["source"] = "sha256:" + archive_sha256
        elif mount["target"] == "/runner/entrypoint.py":
            mount["source"] = "sha256:" + fixture_sha256
        elif mount["target"] == "/runner/network_probe.py":
            mount["source"] = "sha256:" + probe_sha256
    return {"mounts": receipt_mounts, "environment_allowlist": sorted(actual_env),
            "network_mode": "internal", "egress_allowed": False,
            "source_mount_readonly": True, "scratch_mount_writable": True,
            "docker_socket_mounted": False, "host_home_mounted": False,
            "host_credentials_mounted": False, "credential_access": "synthetic_database_only",
            "readonly_fixture_allowlist": FIXTURE_ALLOWLIST}


def _create_run_container(name: str, run_id: str, kind: str, args: list[str],
                          journal: Journal) -> tuple[str, dict]:
    journal.event("container_create_intent", container=name, kind=kind, run_id=run_id)
    result = _docker("run", "--pull=never", "--detach", "--name", name,
                     "--label", RUN_ID_LABEL + "=" + run_id,
                     "--label", KIND_LABEL + "=" + kind, *args, timeout=60)
    container_id = result.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{64}", container_id):
        raise GateError("Docker did not return a full container ID; reconcile by owned name")
    journal.event("container_create_acknowledged", container=name, container_id=container_id, kind=kind)
    return container_id, {}


def _create_network(name: str, run_id: str, journal: Journal) -> str:
    journal.event("network_create_intent", network=name, run_id=run_id, internal=True)
    result = _docker("network", "create", "--internal", "--ipv6=false",
                     "--opt", "com.docker.network.bridge.enable_ip_masquerade=false",
                     "--label", RUN_ID_LABEL + "=" + run_id, name, timeout=30)
    network_id = result.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{64}", network_id):
        raise GateError("Docker did not return a full internal network ID")
    journal.event("network_create_acknowledged", network=name, network_id=network_id)
    return network_id


def _wait_postgres(name: str, deadline: float) -> None:
    while time.monotonic() < deadline:
        result = _docker("exec", name, "pg_isready", "-U", "postgres", "-d", "postgres",
                         check=False, timeout=5)
        if result.returncode == 0:
            return
        time.sleep(0.25)
    raise GateError("Disposable PostgreSQL readiness deadline expired")


def _check_postgres_inspect(row: dict, *, name: str, run_id: str, container_id: str,
                            image_id: str, network: str) -> None:
    _verify_container(row, name=name, run_id=run_id, expected_id=container_id,
                      kind="postgres", image_id=image_id)
    host = row.get("HostConfig", {})
    mounts = row.get("Mounts", [])
    expected_tmpfs = {"/var/lib/postgresql/data", "/var/run/postgresql", "/tmp"}
    actual_tmpfs = {mount.get("Destination") for mount in mounts}
    tmpfs = host.get("Tmpfs") or {}
    log_config = host.get("LogConfig", {})
    networks = row.get("NetworkSettings", {}).get("Networks") or {}
    if (host.get("NetworkMode") != network
            or host.get("MemorySwap") != 1536 * 1024**2 or host.get("NanoCpus") != 1_000_000_000
            or host.get("Memory") != 1536 * 1024**2 or host.get("PidsLimit") != 128
            or host.get("PortBindings") not in ({}, None) or host.get("ReadonlyRootfs") is not True
            or log_config.get("Type") != "local"
            or log_config.get("Config") != {"max-size": "32m", "max-file": "2"}
            or host.get("CapAdd") not in ([], None) or "ALL" not in host.get("CapDrop", [])
            or host.get("SecurityOpt") is None
            or "no-new-privileges:true" not in host.get("SecurityOpt", [])
            or host.get("Tmpfs") is None
            or row.get("Config", {}).get("User") not in ("999:999", "999")
            or set(networks) != {network}
            or len(mounts) != 3 or actual_tmpfs != expected_tmpfs
            or any(mount.get("Type") != "tmpfs" or mount.get("RW") is not True for mount in mounts)
            or "/var/lib/postgresql/data" not in tmpfs
            or "size=1073741824" not in tmpfs["/var/lib/postgresql/data"]
            or "/var/run/postgresql" not in tmpfs
            or "size=16777216" not in tmpfs["/var/run/postgresql"]
            or "/tmp" not in tmpfs or "size=134217728" not in tmpfs["/tmp"]):
        raise GateError("Disposable PostgreSQL resource/network/mount limits differ from plan")


def _check_firewall_inspect(row: dict, *, name: str, run_id: str, container_id: str,
                            kind: str,
                            image_id: str, namespace_id: str, postgres_ip: str,
                            candidate_ip: str, postgres_namespace: bool) -> None:
    _verify_container(row, name=name, run_id=run_id, expected_id=container_id,
                      kind=kind, image_id=image_id)
    host = row.get("HostConfig", {})
    log_config = host.get("LogConfig", {})
    mounts = row.get("Mounts", [])
    if (host.get("NetworkMode") != "container:" + namespace_id
            or host.get("CapAdd") != ["NET_ADMIN"] or "ALL" not in host.get("CapDrop", [])
            or host.get("Privileged") is not False or host.get("ReadonlyRootfs") is not True
            or host.get("Tmpfs") != {"/run": "rw,nosuid,nodev,size=1048576,mode=493"}
            or host.get("Memory") != 128 * 1024**2 or host.get("MemorySwap") != 128 * 1024**2
            or host.get("NanoCpus") != 250_000_000 or host.get("PidsLimit") != 32
            or host.get("PortBindings") not in ({}, None) or len(mounts) != 1
            or mounts[0].get("Type") != "tmpfs" or mounts[0].get("Destination") != "/run"
            or mounts[0].get("RW") is not True
            or log_config.get("Type") != "local"
            or log_config.get("Config") != {"max-size": "4m", "max-file": "2"}
            or row.get("Config", {}).get("Cmd") != [
                "-ec", _firewall_script(postgres_ip, candidate_ip,
                                         postgres_namespace=postgres_namespace)]
            or row.get("Config", {}).get("Entrypoint") != ["/bin/sh"]):
        raise GateError("Firewall sidecar identity, capabilities, command, or caps differ")


def _check_probe_inspect(row: dict, *, name: str, run_id: str, container_id: str,
                         kind: str,
                         image_id: str, candidate_id: str, probe_path: Path,
                         postgres_ip: str, gateway: str, listener_port: int,
                         mode: str, environment: dict[str, str]) -> None:
    _verify_container(row, name=name, run_id=run_id, expected_id=container_id,
                      kind=kind, image_id=image_id)
    config = row.get("Config", {})
    host = row.get("HostConfig", {})
    log_config = host.get("LogConfig", {})
    mounts = row.get("Mounts", [])
    expected = [{"Type": "bind", "Source": str(probe_path), "Destination": "/runner/network_probe.py",
                 "RW": False}]
    actual = [{key: mount.get(key) for key in ("Type", "Source", "Destination", "RW")}
              for mount in mounts]
    actual_environment = {}
    for item in config.get("Env", []):
        if not isinstance(item, str) or "=" not in item:
            raise GateError("Trusted network probe environment is malformed")
        key, value = item.split("=", 1)
        if key in actual_environment:
            raise GateError("Trusted network probe environment contains duplicate names")
        actual_environment[key] = value
    if (host.get("NetworkMode") != "container:" + candidate_id
            or host.get("CapAdd") not in ([], None) or "ALL" not in host.get("CapDrop", [])
            or host.get("Privileged") is not False or host.get("ReadonlyRootfs") is not True
            or host.get("Memory") != 128 * 1024**2 or host.get("MemorySwap") != 128 * 1024**2
            or host.get("NanoCpus") != 250_000_000 or host.get("PidsLimit") != 32
            or host.get("PortBindings") not in ({}, None) or actual != expected
            or log_config.get("Type") != "local"
            or log_config.get("Config") != {"max-size": "4m", "max-file": "2"}
            or actual_environment != environment
            or config.get("Entrypoint") != ["/usr/bin/env"]
            or config.get("Cmd") != ["python3", "/runner/network_probe.py", mode,
                                      postgres_ip, gateway, str(listener_port)]):
        raise GateError("Network probe sidecar isolation or argv differs from plan")


def _write_redacted_log(container_id: str, log_path: Path, password: str | None) -> str:
    raw_path = log_path.with_suffix(".raw")
    descriptor = os.open(raw_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as raw:
            def cap_log_file() -> None:
                resource.setrlimit(resource.RLIMIT_FSIZE, (128 * 1024 * 1024, 128 * 1024 * 1024))
            process = subprocess.Popen(["docker", "logs", container_id],
                                       stdout=raw, stderr=subprocess.STDOUT,
                                       preexec_fn=cap_log_file)
            try:
                result_code = process.wait(timeout=120)
            except subprocess.TimeoutExpired as error:
                process.kill()
                process.wait(timeout=5)
                raise GateError("Docker log collection deadline expired") from error
            raw.flush()
            os.fsync(raw.fileno())
            if result_code != 0 or raw_path.stat().st_size >= 128 * 1024 * 1024:
                raise GateError("Docker log collection failed or exceeded 128 MiB")
        secret = password.encode() if password else b""
        carry = b""
        with raw_path.open("rb") as source, log_path.open("xb") as output:
            while True:
                chunk = source.read(64 * 1024)
                if not chunk:
                    break
                data = carry + chunk
                # Replace complete occurrences before retaining the longest suffix
                # that can still become a secret when the next chunk arrives.
                data = data.replace(secret, b"[REDACTED]") if secret else data
                keep = 0
                if secret:
                    for length in range(1, min(len(secret), len(data) + 1)):
                        if data.endswith(secret[:length]):
                            keep = length
                if keep:
                    output.write(data[:-keep])
                    carry = data[-keep:]
                else:
                    output.write(data)
                    carry = b""
            output.write(carry.replace(secret, b"[REDACTED]") if secret else carry)
            output.flush()
            os.fsync(output.fileno())
    finally:
        try:
            raw_path.unlink()
        except FileNotFoundError:
            pass
    digest = hashlib.sha256()
    with log_path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _log_contains(path: Path, needle: str) -> bool:
    target = needle.encode()
    carry = b""
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(64 * 1024), b""):
            data = carry + chunk
            if target in data:
                return True
            carry = data[-max(0, len(target) - 1):] if len(target) > 1 else b""
    return False


def _sign_receipt(predicate: dict, key_path: Path, key_id: str) -> dict:
    try:
        from trusted_gate_attestation import sign_attestation
    except ImportError as error:
        raise GateError("Trusted attestation signer is unavailable") from error
    return sign_attestation(predicate, key_path=key_path, key_id=key_id)


def _write_receipt(path: Path, receipt: dict) -> None:
    payload = _canonical(receipt) + b"\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _host_memory_check() -> float:
    available = _available_gib()
    if available < RESOURCE_LIMITS["host_required_available_gib"]:
        raise GateError("Host memory is below container caps plus reviewed 8 GiB reserve")
    return available


def _pytest_counts(log_path: Path) -> dict[str, int]:
    with log_path.open("rb") as stream:
        stream.seek(max(0, log_path.stat().st_size - 64 * 1024))
        tail = stream.read(64 * 1024).decode("utf-8", errors="replace")
    tail = re.sub(r"\x1b\[[0-9;]*m", "", tail)
    summaries = [line for line in tail.splitlines()
                 if re.search(r"\bin [0-9]+(?:\.[0-9]+)?s\b", line)
                 and (re.search(r"\b[0-9]+ (?:passed|failed|errors?|skipped|deselected|xfailed|xpassed)\b", line)
                      or "no tests ran" in line)]
    if not summaries:
        raise GateError("Focused validation has no bounded pytest collection summary")
    counts = {name: 0 for name in ("passed", "failed", "errors", "skipped", "deselected", "xfailed", "xpassed")}
    for number, name in re.findall(r"\b([0-9]+) (passed|failed|errors?|skipped|deselected|xfailed|xpassed)\b", summaries[-1]):
        counts["errors" if name == "error" else name] += int(number)
    counts["selected"] = sum(value for name, value in counts.items() if name != "deselected")
    counts["collected"] = counts["selected"] + counts["deselected"]
    return counts


def execute(checkout: Path, predicate_path: Path, go_path: Path | None, key_path: Path,
            key_id: str, output_dir: Path, timeout: int = 3600, *,
            _policy_authorization: gate_policy.Authorization | None = None) -> dict:
    global _ACTIVE_POLICY_AUTHORIZATION
    focused = _policy_authorization.focused if _policy_authorization is not None else None
    predicate = _read_predicate(predicate_path, focused=focused is not None)
    if (_policy_authorization is not None
            and _digest_bytes(_canonical(predicate)) != _policy_authorization.predicate_sha256):
        raise GateError("Expected predicate changed after one-shot authorization")
    plan = prepare(checkout, predicate_path, _focused=focused is not None)
    command = gate_policy.FOCUSED_COMMANDS[focused["profile"]] if focused is not None else DEFAULT_GATE_COMMAND
    command_sha256 = _digest_bytes(json.dumps(command).encode())
    if focused is not None:
        plan["gate"] = {**plan["gate"], "argv": command, "command_sha256": command_sha256,
                        "focused_profile": focused["profile"], "stage": focused["stage"]}
    if plan["policy"]["predicate_sha256"] != _digest_bytes(_canonical(predicate)):
        raise GateError("Expected predicate changed while the reviewed plan was prepared")
    plan_sha256 = plan_digest(plan)
    runner_provenance = plan["runner_source"]
    if _policy_authorization is None:
        if go_path is None:
            raise GateError("Manual execution requires exact reviewed GO")
        go = _read_json(go_path, max_bytes=16 * 1024)
    else:
        if (go_path is not None or not isinstance(_policy_authorization, gate_policy.Authorization)
                or _policy_authorization.policy["runner_source"] != runner_provenance):
            raise GateError("One-shot authorization differs from frozen runner source")
        _policy_authorization.check(starting=True)
        go = None
    if go is not None and (go.get("decision") != "GO" or go.get("task_id") != TASK_ID
            or go.get("plan_sha256") != plan_sha256
            or go.get("predicate_sha256") != plan["policy"]["predicate_sha256"]
            or go.get("runner_image_id") != predicate["runner_image_id"]
            or go.get("postgres_image_id") != predicate["postgres_image_id"]
            or go.get("firewall_image_id") != predicate["firewall_image_id"]
            or go.get("firewall_policy_sha256") != FIREWALL_POLICY_SHA256
            or go.get("network_probe_sha256") != predicate["network_probe_sha256"]
            or go.get("execution_host") != predicate["execution_host"]
            or go.get("runner_commit") != runner_provenance["commit"]
            or go.get("runner_tree") != runner_provenance["tree"]
            or go.get("runner_script_sha256") != runner_provenance["script_sha256"]
            or go.get("entrypoint_sha256") != runner_provenance["entrypoint_sha256"]
            or go.get("network_probe_sha256") != runner_provenance["network_probe_sha256"]
            or go.get("attestation_signer_sha256") != runner_provenance["attestation_signer_sha256"]
            or go.get("attestation_key_id") != key_id
            or not isinstance(go.get("reviewer"), str) or not go["reviewer"].strip()
            or not isinstance(go.get("root_authorizer"), str) or not go["root_authorizer"].strip()
            or go["reviewer"].strip() == go["root_authorizer"].strip()):
        raise GateError("Matching independent-review and root GO record is required before Docker")
    if platform.node().split(".", 1)[0].lower() != predicate["execution_host"]:
        raise GateError("Execution hostname differs from the frozen full-test predicate")
    if timeout <= 0 or timeout > RESOURCE_LIMITS["candidate"]["timeout_seconds"]:
        raise GateError("Gate timeout exceeds its reviewed limit")
    if _runner_provenance() != runner_provenance:
        raise GateError("Trusted runner source changed after the reviewed GO record")
    _validate_attestation_key(key_path, key_id)
    available_gib = _host_memory_check()
    output_dir = _ensure_private_output(output_dir)
    run_id = uuid4().hex
    run_dir = output_dir / ("run-" + run_id)
    run_dir.mkdir(mode=0o700)
    journal = Journal(run_dir / "runner.journal.jsonl", {
        "task_id": focused["task"]["task_id"] if focused is not None else TASK_ID,
        "run_id": run_id, "execution_host": predicate["execution_host"],
        "candidate_commit": predicate["candidate_commit"], "candidate_tree": predicate["candidate_tree"],
        "candidate_archive_sha256": predicate["candidate_archive_sha256"],
        "plan_sha256": plan_sha256, "available_gib": round(available_gib, 2),
        "one_shot_consumption_sha256": (_digest_bytes(_policy_authorization.record.read_bytes())
                                         if _policy_authorization is not None else None),
    })
    archive_path = run_dir / "candidate.tar"
    archive_root = run_dir / "candidate-source"
    entrypoint_source = Path(__file__).with_name("gate_container_entrypoint.py")
    fixture_path = run_dir / "entrypoint.py"
    probe_source = Path(__file__).with_name("gate_network_probe.py")
    probe_path = run_dir / "network_probe.py"
    resource_names = {
        "network": "skybuild-full-test-net-" + run_id,
        "postgres": "skybuild-full-test-postgres-" + run_id,
        "candidate": "skybuild-full-test-candidate-" + run_id,
        "candidate_firewall": "skybuild-full-test-candidate-fw-" + run_id,
        "postgres_firewall": "skybuild-full-test-postgres-fw-" + run_id,
        "candidate_probe": "skybuild-full-test-candidate-probe-" + run_id,
        "postgres_probe": "skybuild-full-test-postgres-probe-" + run_id,
    }
    postgres_password = secrets.token_urlsafe(32)
    attempted = {"network": False, "postgres": False, "candidate": False,
                 "candidate_firewall": False, "postgres_firewall": False,
                 "candidate_probe": False, "postgres_probe": False}
    ids = {"network": None, "postgres": None, "candidate": None,
           "candidate_firewall": None, "postgres_firewall": None,
           "candidate_probe": None, "postgres_probe": None}
    started_at = _now()
    deadline = time.monotonic() + timeout
    exit_code = 1
    log_sha256 = None
    mount_evidence = None
    preflight_evidence = None
    firewall_evidence: dict[str, bool] = {}
    network_probe_evidence = None
    postgres_probe_evidence = None
    host_listener: _OwnedHostListener | None = None
    cleanup_ok = True
    cleanup_results = {}
    failure = None
    try:
        candidate = _candidate_identity(Path(checkout), predicate)
        archive_sha256, archive_size = _archive(Path(candidate["checkout"]), candidate["commit"], archive_path)
        if archive_sha256 != predicate["candidate_archive_sha256"]:
            raise GateError("Candidate archive changed after GO")
        _extract_archive(archive_path, archive_root)
        history_sha256, _ = _history_fixture(Path(candidate["checkout"]), archive_root / ".git")
        if history_sha256 != predicate["candidate_history_sha256"]:
            raise GateError("Sanitized history fixture changed after GO")
        archive_root.chmod(0o755)
        entry_info = entrypoint_source.lstat()
        if not stat.S_ISREG(entry_info.st_mode) or entry_info.st_uid != os.geteuid():
            raise GateError("Trusted entrypoint fixture is not an owned regular file")
        shutil.copyfile(entrypoint_source, fixture_path, follow_symlinks=False)
        fixture_path.chmod(0o555)
        entrypoint_sha256 = _digest_bytes(fixture_path.read_bytes())
        if entrypoint_sha256 != predicate["trusted_entrypoint_sha256"]:
            raise GateError("Trusted entrypoint changed after reviewed GO")
        shutil.copyfile(probe_source, probe_path, follow_symlinks=False)
        probe_path.chmod(0o555)
        probe_sha256 = _digest_bytes(probe_path.read_bytes())
        if probe_sha256 != predicate["network_probe_sha256"]:
            raise GateError("Trusted network probe changed after reviewed GO")
        runner_image = _inspect_image(predicate["runner_image_id"])
        pg_image = _inspect_image(predicate["postgres_image_id"])
        firewall_image = _inspect_image(predicate["firewall_image_id"])
        _validate_runner_image(runner_image, predicate, archive_root, entrypoint_sha256)
        if pg_image.get("Id") != predicate["postgres_image_id"]:
            raise GateError("Exact disposable PostgreSQL image is not present")
        _validate_firewall_image(firewall_image, predicate)
        # Bind the fixed process environment to this candidate before any source runs.
        env = _candidate_environment(postgres_password)
        env["SKYBUILD_GATE_HOST_GATEWAY"] = "pending"
        env["SKYBUILD_GATE_HOST_LISTENER_PORT"] = "pending"
        attempted["network"] = True
        ids["network"] = _create_network(resource_names["network"], run_id, journal)
        network_data = _network_info(resource_names["network"], run_id, ids["network"])
        if network_data is None:
            raise GateError("Owned internal network disappeared after creation")
        try:
            gateway = str(ipaddress.ip_address(
                ((network_data.get("IPAM") or {}).get("Config") or [{}])[0].get("Gateway", "")))
        except ValueError as error:
            raise GateError("Internal network gateway could not be identified") from error
        if ipaddress.ip_address(gateway).version != 4 or network_data.get("EnableIPv6") is True:
            raise GateError("Internal network gateway could not be identified for hostile probe")
        host_listener = _OwnedHostListener(gateway)
        env["SKYBUILD_GATE_HOST_GATEWAY"] = gateway
        env["SKYBUILD_GATE_HOST_LISTENER_PORT"] = str(host_listener.port)
        attempted["postgres"] = True
        pg_args = [
            "--network", resource_names["network"], "--memory=1536m", "--memory-swap=1536m",
            "--cpus=1", "--pids-limit=128", "--read-only", "--cap-drop=ALL",
            "--log-driver=local", "--log-opt=max-size=32m", "--log-opt=max-file=2",
            "--security-opt=no-new-privileges", "--user", "999:999",
            "--tmpfs", "/var/lib/postgresql/data:rw,nosuid,nodev,noexec,size=1073741824,uid=999,gid=999,mode=0700",
            "--tmpfs", "/var/run/postgresql:rw,nosuid,nodev,noexec,size=16777216,uid=999,gid=999,mode=3775",
            "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=134217728,uid=999,gid=999,mode=1777",
            "--env", "POSTGRES_USER=postgres", "--env", "POSTGRES_PASSWORD=" + postgres_password,
            predicate["postgres_image_id"],
        ]
        ids["postgres"], _ = _create_run_container(resource_names["postgres"], run_id,
                                                    "postgres", pg_args, journal)
        pg_row = _container_info(resource_names["postgres"], run_id, ids["postgres"],
                                 "postgres", predicate["postgres_image_id"])
        if pg_row is None:
            raise GateError("Disposable PostgreSQL container disappeared after creation")
        _check_postgres_inspect(pg_row, name=resource_names["postgres"], run_id=run_id,
                                container_id=ids["postgres"], image_id=predicate["postgres_image_id"],
                                network=resource_names["network"])
        _wait_postgres(resource_names["postgres"], min(deadline, time.monotonic() + 90))
        journal.event("postgres_local_unix_socket_ready", container_id=ids["postgres"],
                      probe="pg_isready via docker exec with default container Unix socket")
        for database in ("skybuild_test", "skybuild_http_test", "skybuild_import_test"):
            _docker("exec", ids["postgres"], "createdb", "-U", "postgres", database, timeout=20)
            journal.event("synthetic_database_created", database=database)
        pg_ip = _container_ipv4(pg_row, resource_names["network"])
        env["SKYBUILD_GATE_HOST_GATEWAY"] = gateway
        candidate_env = dict(env)
        candidate_args = [
            "--network", resource_names["network"], "--add-host", "db:" + pg_ip,
            "--memory=4g", "--memory-swap=4g",
            "--cpus=2", "--pids-limit=256", "--shm-size=256m", "--read-only",
            "--log-driver=local", "--log-opt=max-size=128m", "--log-opt=max-file=2",
            "--cap-drop=ALL", "--security-opt=no-new-privileges", "--user", "10001:10001",
            "--workdir", "/scratch", "--mount",
            f"type=bind,src={archive_root},dst=/candidate,readonly", "--tmpfs",
            "/scratch:rw,exec,nosuid,nodev,size=2g,uid=10001,gid=10001,mode=0700", "--mount",
            f"type=bind,src={fixture_path},dst=/runner/entrypoint.py,readonly",
            "--mount", f"type=bind,src={probe_path},dst=/runner/network_probe.py,readonly",
        ]
        for name, value in candidate_env.items():
            candidate_args.extend(("--env", name + "=" + value))
        candidate_args.extend(("--entrypoint", "/runner/entrypoint.py", predicate["runner_image_id"],
                               *command))
        attempted["candidate"] = True
        ids["candidate"], _ = _create_run_container(resource_names["candidate"], run_id,
                                                      "candidate", candidate_args, journal)
        candidate_row = _container_info(resource_names["candidate"], run_id, ids["candidate"],
                                        "candidate", predicate["runner_image_id"])
        if candidate_row is None:
            raise GateError("Candidate container disappeared after creation")
        mount_evidence = _check_candidate_inspect(candidate_row, name=resource_names["candidate"],
                                                  run_id=run_id, container_id=ids["candidate"],
                                                  image_id=predicate["runner_image_id"],
                                                  archive_root=archive_root, fixture_path=fixture_path,
                                                  probe_path=probe_path, env=candidate_env,
                                                  network=resource_names["network"],
                                                  postgres_ip=pg_ip,
                                                  archive_sha256=archive_sha256,
                                                  fixture_sha256=entrypoint_sha256,
                                                  probe_sha256=probe_sha256, command=command)
        journal.event("candidate_isolation_verified", container_id=ids["candidate"],
                      image_id=predicate["runner_image_id"], mounts=mount_evidence["mounts"],
                      environment_allowlist=mount_evidence["environment_allowlist"])
        candidate_ip = _container_ipv4(candidate_row, resource_names["network"])
        _wait_candidate_blocked(resource_names["candidate"], run_id, ids["candidate"],
                                predicate["runner_image_id"], journal)
        # Separate NET_ADMIN sidecars install and then prove the policy in both
        # network namespaces. The candidate image never receives these caps.
        for key, image_id, namespace_id, pg_namespace in (
                ("postgres_firewall", predicate["firewall_image_id"], ids["postgres"], True),
                ("candidate_firewall", predicate["firewall_image_id"], ids["candidate"], False)):
            attempted[key] = True
            sidecar_args = [
                "--network=container:" + namespace_id, "--memory=128m", "--memory-swap=128m",
                "--cpus=0.25", "--pids-limit=32", "--read-only", "--cap-drop=ALL",
                "--tmpfs", "/run:rw,nosuid,nodev,size=1m,mode=0755",
                "--log-driver=local", "--log-opt=max-size=4m", "--log-opt=max-file=2",
                "--cap-add=NET_ADMIN", "--security-opt=no-new-privileges", "--entrypoint", "/bin/sh",
                image_id, "-ec", _firewall_script(pg_ip, candidate_ip, postgres_namespace=pg_namespace),
            ]
            ids[key], _ = _create_run_container(resource_names[key], run_id, key,
                                                 sidecar_args, journal)
            sidecar = _container_info(resource_names[key], run_id, ids[key], key, image_id)
            if sidecar is None or not sidecar.get("State", {}).get("Running"):
                raise GateError("Firewall sidecar did not remain active")
            _check_firewall_inspect(sidecar, name=resource_names[key], run_id=run_id,
                                    container_id=ids[key], kind=key, image_id=image_id,
                                    namespace_id=namespace_id,
                                    postgres_ip=pg_ip, candidate_ip=candidate_ip,
                                    postgres_namespace=pg_namespace)
            rules = _verify_firewall_rules(ids[key], pg_ip, candidate_ip,
                                           postgres_namespace=pg_namespace)
            firewall_evidence.update(rules)
            journal.event("namespace_firewall_verified", namespace=key,
                          sidecar_id=ids[key], image_id=image_id,
                          policy_sha256=FIREWALL_POLICY_SHA256, **rules)
        for key, namespace_id, mode in (
                ("postgres_probe", ids["postgres"], "postgres"),
                ("candidate_probe", ids["candidate"], "candidate")):
            attempted[key] = True
            probe_environment = _trusted_probe_environment()
            probe_args = [
                "--network=container:" + namespace_id, "--memory=128m", "--memory-swap=128m",
                "--cpus=0.25", "--pids-limit=32", "--read-only", "--cap-drop=ALL",
                "--log-driver=local", "--log-opt=max-size=4m", "--log-opt=max-file=2",
                "--security-opt=no-new-privileges", "--mount",
                f"type=bind,src={probe_path},dst=/runner/network_probe.py,readonly",
            ]
            for env_name, env_value in probe_environment.items():
                probe_args.extend(("--env", env_name + "=" + env_value))
            probe_args.extend(("--entrypoint", "/usr/bin/env", predicate["runner_image_id"], "python3",
                "/runner/network_probe.py", mode, pg_ip, gateway, str(host_listener.port),
            ))
            ids[key], _ = _create_run_container(resource_names[key], run_id, key,
                                                 probe_args, journal)
            probe_row = _container_info(resource_names[key], run_id, ids[key], key,
                                        predicate["runner_image_id"])
            if probe_row is None:
                raise GateError("Trusted network probe container disappeared")
            _check_probe_inspect(probe_row, name=resource_names[key], run_id=run_id,
                                 container_id=ids[key], kind=key,
                                 image_id=predicate["runner_image_id"],
                                 candidate_id=namespace_id, probe_path=probe_path,
                                 postgres_ip=pg_ip, gateway=gateway,
                                 listener_port=host_listener.port, mode=mode,
                                 environment=probe_environment)
            probe_exit = _wait_exit(resource_names[key], run_id, ids[key], key,
                                    predicate["runner_image_id"], min(deadline, time.monotonic() + 30))
            probe_log = run_dir / (key + ".log")
            probe_log_sha256 = _write_redacted_log(ids[key], probe_log, None)
            if probe_exit != 0:
                raise GateError("Trusted namespace network probes rejected the firewall")
            probe_line = next((line for line in probe_log.read_text(encoding="utf-8").splitlines()
                               if line.startswith("GATE_NETWORK_PROBE=")), "")
            try:
                probe_result = json.loads(probe_line.split("=", 1)[1])
            except (IndexError, json.JSONDecodeError) as error:
                raise GateError("Trusted namespace network probe output is invalid") from error
            expected_blocked = {"dns_blocked": True, "external_ipv4_blocked": True,
                                "external_ipv6_blocked": True,
                                "host_gateway_listener_blocked": True,
                                "host_listener_port": host_listener.port}
            if (any(probe_result.get(field) != value for field, value in expected_blocked.items())
                    or probe_result.get("namespace") != mode
                    or (mode == "candidate" and probe_result.get("postgres_tcp_allowed") is not True)):
                raise GateError("Trusted namespace probe did not prove exact allow/deny behavior")
            probe_result.pop("namespace", None)
            probe_result["log_sha256"] = probe_log_sha256
            if mode == "candidate":
                network_probe_evidence = {
                    "postgres_tcp_allowed": probe_result["postgres_tcp_allowed"],
                    "dns_blocked": probe_result["dns_blocked"],
                    "external_ipv4_blocked": probe_result["external_ipv4_blocked"],
                    "external_ipv6_blocked": probe_result["external_ipv6_blocked"],
                    "host_gateway_listener_blocked": probe_result["host_gateway_listener_blocked"],
                    "host_listener_port": probe_result["host_listener_port"],
                    "log_sha256": probe_result["log_sha256"],
                }
            else:
                postgres_probe_evidence = {
                    "dns_blocked": probe_result["dns_blocked"],
                    "external_ipv4_blocked": probe_result["external_ipv4_blocked"],
                    "external_ipv6_blocked": probe_result["external_ipv6_blocked"],
                    "host_gateway_listener_blocked": probe_result["host_gateway_listener_blocked"],
                    "host_listener_port": probe_result["host_listener_port"],
                    "postgres_unix_socket_ready": True,
                    "log_sha256": probe_result["log_sha256"],
                }
            journal.event("namespace_network_probe_passed", namespace=mode,
                          container_id=ids[key], exit_code=probe_exit,
                          log_sha256=probe_log_sha256, port=host_listener.port)
        if not (network_probe_evidence and postgres_probe_evidence):
            raise GateError("Both candidate and PostgreSQL namespaces must pass trusted probes")
        _docker("exec", ids["candidate"], "python3", "-c",
                "from pathlib import Path; Path('/scratch/.firewall-ready').write_text('firewall-ready\\n', encoding='ascii')",
                timeout=10)
        journal.event("candidate_firewall_release", candidate_id=ids["candidate"],
                      probe_ids=[ids["postgres_probe"], ids["candidate_probe"]],
                      listener_port=host_listener.port)
        while time.monotonic() < deadline:
            if _policy_authorization is not None:
                _policy_authorization.check()
            current = _container_info(resource_names["candidate"], run_id, ids["candidate"],
                                      "candidate", predicate["runner_image_id"])
            if current is None:
                raise GateError("Candidate container vanished before exit status was observed")
            if current.get("State", {}).get("Status") == "exited":
                exit_code = current.get("State", {}).get("ExitCode")
                if type(exit_code) is not int or exit_code < 0:
                    raise GateError("Candidate exit status is malformed")
                break
            time.sleep(0.5)
        else:
            raise GateError("Full test gate exceeded its reviewed wall-clock deadline")
        log_path = run_dir / "candidate.log"
        log_sha256 = _write_redacted_log(ids["candidate"], log_path, postgres_password)
        markers = (
            "GATE_CANDIDATE_BLOCKED=firewall_release_pending",
            "GATE_FIREWALL_RELEASE=verified",
            "GATE_CANDIDATE_ARCHIVE_READONLY=true",
            "GATE_CANDIDATE_COPY=complete",
            "GATE_OFFLINE_ENVIRONMENT=prepared",
            "GATE_PREFLIGHT_SOURCE_PATH=/scratch/workspace/src/skybuild/__init__.py",
            "GATE_COMMAND_LAUNCH=trusted_exec",
            "GATE_HOST_GATEWAY_PROBE=blocked",
            "GATE_EXTERNAL_DIRECT_IP_PROBE=blocked",
            "GATE_EXTERNAL_DNS_PROBE=blocked",
        )
        if any(not _log_contains(log_path, marker) for marker in markers):
            raise GateError("Candidate network isolation hostile probe did not pass")
        preflight_evidence = {
            "candidate_archive_readonly": True,
            "candidate_copied_to_scratch": True,
            "source_package_path": "/scratch/workspace/src/skybuild/__init__.py",
            "gate_launch": "trusted_exec",
            "host_gateway_probe": "blocked",
            "external_direct_ip_probe": "blocked",
            "external_dns_probe": "blocked",
        }
        journal.event("candidate_gate_finished", container_id=ids["candidate"],
                      exit_code=exit_code, log_sha256=log_sha256)
    except Exception as error:
        # Admission failure must not prevent trusted stop/log/cleanup commands.
        if _policy_authorization is not None:
            _ACTIVE_POLICY_AUTHORIZATION = None
        exit_code = 1
        failure = type(error).__name__
        journal.event("gate_execution_failed", error=failure)
        if ids["candidate"] and attempted["candidate"]:
            try:
                row = _container_info(resource_names["candidate"], run_id, ids["candidate"], "candidate")
                if row and row.get("State", {}).get("Running"):
                    _docker("stop", "--time", "10", ids["candidate"], timeout=20)
                log_path = run_dir / "candidate.log"
                if not log_path.exists():
                    log_sha256 = _write_redacted_log(ids["candidate"], log_path, postgres_password)
            except Exception as cleanup_error:
                cleanup_results["candidate_logs"] = {"confirmed": False, "error": type(cleanup_error).__name__}
    finally:
        if _policy_authorization is not None:
            _ACTIVE_POLICY_AUTHORIZATION = None
        cleanup_order = (
            ("candidate", "candidate"), ("candidate_probe", "candidate_probe"),
            ("candidate_firewall", "candidate_firewall"),
            ("postgres_probe", "postgres_probe"), ("postgres_firewall", "postgres_firewall"),
            ("postgres", "postgres"),
        )
        for name, kind in cleanup_order:
            if attempted[kind]:
                try:
                    cleanup_results[name] = {"confirmed": _remove_container(resource_names[name], run_id,
                                                                            ids[kind], kind, journal)}
                except Exception as error:
                    cleanup_ok = False
                    cleanup_results[name] = {"confirmed": False, "error": type(error).__name__}
                    journal.event("cleanup_unconfirmed", resource=name, error=type(error).__name__)
        if host_listener is not None:
            try:
                host_listener.close()
                cleanup_results["host_listener"] = {"confirmed": True,
                                                      "port": host_listener.port}
                journal.event("host_listener_closed", port=host_listener.port)
            except Exception as error:
                cleanup_ok = False
                cleanup_results["host_listener"] = {"confirmed": False, "error": type(error).__name__}
                journal.event("cleanup_unconfirmed", resource="host_listener", error=type(error).__name__)
        if attempted["network"]:
            try:
                cleanup_results["network"] = {"confirmed": _remove_network(resource_names["network"], run_id,
                                                                            ids["network"], journal)}
            except Exception as error:
                cleanup_ok = False
                cleanup_results["network"] = {"confirmed": False, "error": type(error).__name__}
                journal.event("cleanup_unconfirmed", resource="network", error=type(error).__name__)
        if not cleanup_ok:
            exit_code = 1
    if _policy_authorization is not None:
        try:
            _policy_authorization.check()
        except Exception as error:
            exit_code = 1
            failure = type(error).__name__
            journal.event("policy_watch_failed_before_signing", error=failure)
    journal.event("run_finished", exit_code=exit_code, cleanup_confirmed=cleanup_ok,
                  failure=failure)
    journal.close()
    finished_at = _now()
    required_ids = (ids["candidate"], ids["postgres"], ids["network"],
                    ids["candidate_firewall"], ids["candidate_probe"],
                    ids["postgres_firewall"], ids["postgres_probe"])
    if (failure is not None or (exit_code != 0 and focused is None) or not cleanup_ok or any(not isinstance(value, str) for value in required_ids)
            or not log_sha256 or not mount_evidence or not preflight_evidence
            or not network_probe_evidence or not postgres_probe_evidence):
        return {"run_directory": str(run_dir), "attestation": None,
                "exit_code": exit_code, "cleanup_confirmed": cleanup_ok,
                "log_sha256": log_sha256, "failure": failure}
    result = {
        "gate_argv": command,
        "gate_command_sha256": command_sha256,
        "gate_policy_sha256": predicate["gate_policy_sha256"],
        "target_ref": predicate["target_ref"], "target_base": predicate["target_base"],
        "candidate_commit": predicate["candidate_commit"], "candidate_tree": predicate["candidate_tree"],
        "candidate_archive_sha256": predicate["candidate_archive_sha256"],
        "candidate_history_sha256": predicate["candidate_history_sha256"],
        "runner_identity": predicate["runner_identity"], "runner_version": RUNNER_VERSION,
        "runner_image_id": predicate["runner_image_id"], "execution_host": predicate["execution_host"],
        "postgres_image_id": predicate["postgres_image_id"],
        "firewall_image_id": predicate["firewall_image_id"],
        "firewall_policy_sha256": FIREWALL_POLICY_SHA256,
        "network_probe_sha256": predicate["network_probe_sha256"],
        "trusted_entrypoint_sha256": predicate["trusted_entrypoint_sha256"],
        "attestation_signer_sha256": predicate["attestation_signer_sha256"],
        "candidate_blocked_until_probe": True,
        "firewall_defaults_drop": firewall_evidence.get("firewall_defaults_drop") is True,
        "firewall_ipv4_default_drop": firewall_evidence.get("firewall_ipv4_default_drop") is True,
        "firewall_ipv6_default_drop": firewall_evidence.get("firewall_ipv6_default_drop") is True,
        "firewall_policy_applied": firewall_evidence.get("firewall_policy_applied") is True,
        "postgres_namespace_egress_blocked": bool(postgres_probe_evidence),
        "postgres_network_probe": postgres_probe_evidence,
        "network_probe": network_probe_evidence,
        "postgres_data_mount": {"type": "tmpfs", "source": "tmpfs", "size_bytes": 1073741824},
        "source_mount_readonly": bool(mount_evidence and mount_evidence["source_mount_readonly"]),
        "scratch_mount_writable": bool(mount_evidence and mount_evidence["scratch_mount_writable"]),
        "docker_socket_mounted": False, "host_home_mounted": False,
        "host_credentials_mounted": False, "credential_access": "synthetic_database_only",
        "network_mode": "internal", "egress_allowed": False,
        "environment_allowlist": ENV_ALLOWLIST,
        "readonly_fixture_allowlist": FIXTURE_ALLOWLIST,
        "mounts": mount_evidence["mounts"] if mount_evidence else [],
        "resource_limits": ATTESTED_CANDIDATE_LIMITS,
        "resources": {"runner_container_id": ids["candidate"], "pg_container_id": ids["postgres"],
                      "firewall_container_id": ids["candidate_firewall"],
                      "probe_container_id": ids["candidate_probe"],
                      "postgres_firewall_container_id": ids["postgres_firewall"],
                      "postgres_probe_container_id": ids["postgres_probe"],
                      "network_id": ids["network"],
                      "test_image_id": predicate["runner_image_id"],
                      "postgres_image_id": predicate["postgres_image_id"],
                      "firewall_image_id": predicate["firewall_image_id"]},
        "result": {"exit_code": exit_code, "log_sha256": log_sha256,
                   "started_at": started_at, "finished_at": finished_at,
                   "preflight": preflight_evidence},
        "cleanup": {
            "status": "confirmed",
            "owned_resources": [
                {"kind": kind, "id": resource_id, "state": "absent"}
                for kind, resource_id in (
                    ("runner_container", ids["candidate"]), ("pg_container", ids["postgres"]),
                    ("network", ids["network"]), ("firewall_container", ids["candidate_firewall"]),
                    ("probe_container", ids["candidate_probe"]),
                    ("postgres_firewall_container", ids["postgres_firewall"]),
                    ("postgres_probe_container", ids["postgres_probe"]),
                )
            ],
        },
    }
    if focused is None:
        result.update({"bundle_id": predicate["bundle_id"], "pr_number": predicate["pr_number"]})
        receipt = _sign_receipt(result, key_path, key_id)
    else:
        receipt = gate_policy.sign_focused(result, _policy_authorization, key_path, key_id,
                                          _pytest_counts(run_dir / "candidate.log"))
    attestation_path = run_dir / "attestation.json"
    _write_receipt(attestation_path, receipt)
    return {"run_directory": str(run_dir), "attestation": str(attestation_path),
            "exit_code": exit_code, "cleanup_confirmed": cleanup_ok,
            "log_sha256": log_sha256, "failure": failure,
            "validation_passed": receipt["payload"]["verdict"] == "pass" if focused is not None else True}


def execute_policy(checkout: Path, predicate_path: Path, policy_path: Path,
                   policy_sha256: str, trust_path: Path, trust_sha256: str, input_path: Path,
                   key_path: Path, key_id: str, output_dir: Path, timeout: int = 3600, *,
                   focused_stage: str | None = None) -> dict:
    global _ACTIVE_POLICY_AUTHORIZATION
    if _ACTIVE_POLICY_AUTHORIZATION is not None:
        raise GateError("Only one policy gate may execute in a supervisor process")
    authorization = gate_policy.authorize(
        checkout, _read_predicate(predicate_path, focused=focused_stage is not None), key_id, policy_path, policy_sha256,
        trust_path, trust_sha256, input_path, _runner_provenance(), RESOURCE_LIMITS, focused_stage=focused_stage)
    try:
        authorization.start()
        _ACTIVE_POLICY_AUTHORIZATION = authorization
        result = execute(checkout, predicate_path, None, key_path, key_id, output_dir, timeout,
                         _policy_authorization=authorization)
        result["policy_consumption"] = {
            "path": str(authorization.record),
            "sha256": _digest_bytes(gate_policy.private(authorization.record)),
            "input_sha256": authorization.input_sha256,
        }
        return result
    finally:
        _ACTIVE_POLICY_AUTHORIZATION = None
        authorization.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--expected-predicate", type=Path, required=True)
    parser.add_argument("--runner-image-id")
    parser.add_argument("--postgres-image-id")
    parser.add_argument("--reviewed-go-record", type=Path)
    parser.add_argument("--attestation-key", type=Path)
    parser.add_argument("--attestation-key-id")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--execute-policy", action="store_true")
    parser.add_argument("--policy-permit", type=Path)
    parser.add_argument("--policy-permit-sha256")
    parser.add_argument("--policy-trust", type=Path)
    parser.add_argument("--policy-trust-sha256")
    parser.add_argument("--policy-input", type=Path)
    parser.add_argument("--focused-stage", choices=["focused-0-unit", "focused-0-long", "focused-1-unit", "focused-1-long"])
    args = parser.parse_args(argv)
    try:
        policy_options = (args.policy_permit, args.policy_permit_sha256, args.policy_trust,
                          args.policy_trust_sha256, args.policy_input)
        if args.execute_policy:
            if (args.execute or args.reviewed_go_record or not all(policy_options)
                    or not args.attestation_key or not args.attestation_key_id
                    or args.runner_image_id or args.postgres_image_id):
                raise GateError("Policy execution requires only signed one-shot authority and attestation key pins")
            result = execute_policy(args.checkout, args.expected_predicate,
                                    args.policy_permit, args.policy_permit_sha256,
                                    args.policy_trust, args.policy_trust_sha256, args.policy_input,
                                    args.attestation_key, args.attestation_key_id, args.output_dir, args.timeout,
                                    focused_stage=args.focused_stage)
        elif any(policy_options) or args.focused_stage:
            raise GateError("Policy inputs require explicit --execute-policy")
        elif not args.execute:
            plan = prepare(args.checkout, args.expected_predicate,
                           args.runner_image_id, args.postgres_image_id)
            print(json.dumps({"prepared": True, "plan": plan,
                              "plan_sha256": plan_digest(plan)}, sort_keys=True, indent=2))
            return 0
        else:
            if (not args.reviewed_go_record or not args.attestation_key or not args.attestation_key_id):
                raise GateError("Execution requires reviewed GO, private attestation key, and pinned key ID")
            result = execute(args.checkout, args.expected_predicate, args.reviewed_go_record,
                             args.attestation_key, args.attestation_key_id, args.output_dir, args.timeout)
        print(json.dumps({"executed": True, **result}, sort_keys=True))
        return 0 if (result["exit_code"] == 0 and result["cleanup_confirmed"]
                     and result["attestation"] and result["failure"] is None
                     and result.get("validation_passed", True)) else 1
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError,
            GateError, gate_policy.PolicyError) as error:
        print(json.dumps({"executed": False, "error": type(error).__name__, "detail": str(error)},
                         sort_keys=True), file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
