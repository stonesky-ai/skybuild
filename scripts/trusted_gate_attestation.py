#!/usr/bin/env python3
"""Verify host-signed evidence from the isolated full-test runner.

The key is configured by the trusted publisher, never read from the receipt.
The runner signs only after its supervisor has observed the exact candidate
test exit, log digest, sandbox configuration and cleanup outcome.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
from datetime import datetime
from typing import Mapping
from typing import Any


SCHEMA = "skybuild.full-test-gate-attestation.v1"
MAX_ATTESTATION_BYTES = 64 * 1024
MAX_KEY_BYTES = 4096
_SHA256 = re.compile(r"[0-9a-f]{64}")
_OID = re.compile(r"[0-9a-f]{40}")
_BUNDLE_ID = re.compile(r"bundle-[0-9a-f]{24}")
_REF = re.compile(r"refs/heads/[A-Za-z0-9._/-]{1,220}")
_CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,127}")
PINNED_PREDICATE_FIELDS = {
    "bundle_id", "pr_number", "target_ref", "target_base", "candidate_commit",
    "candidate_tree", "candidate_archive_sha256", "gate_argv", "gate_command_sha256",
    "gate_policy_sha256", "runner_identity", "runner_version", "runner_image_id",
    "postgres_image_id", "firewall_image_id", "firewall_policy_sha256",
    "trusted_entrypoint_sha256", "network_probe_sha256", "attestation_signer_sha256",
    "execution_host",
    "environment_allowlist", "resource_limits",
}


class AttestationError(ValueError):
    """The gate receipt is malformed, untrusted or bound to another input."""


def canonical_bytes(receipt: dict[str, Any]) -> bytes:
    """Canonical signed bytes: sorted, compact UTF-8 JSON without signature."""
    unsigned = {key: value for key, value in receipt.items() if key != "signature"}
    try:
        return json.dumps(unsigned, sort_keys=True, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise AttestationError("Gate attestation is not canonical JSON") from error


def _object(value: Any, fields: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != fields:
        raise AttestationError(f"Gate attestation {label} has missing or unknown fields")
    return value


def _text(value: Any, label: str, limit: int = 512) -> None:
    if (not isinstance(value, str) or not value or len(value) > limit
            or any(ord(char) < 32 for char in value)):
        raise AttestationError(f"Gate attestation {label} is invalid")


def _digest(value: Any, label: str) -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise AttestationError(f"Gate attestation {label} must be a lowercase SHA-256")


def _validate_predicate(value: Any) -> dict:
    keys = {
        "bundle_id", "pr_number", "target_ref", "target_base", "candidate_commit",
        "candidate_tree", "candidate_archive_sha256", "gate_argv", "gate_command_sha256",
        "gate_policy_sha256", "runner_identity", "runner_version", "runner_image_id",
        "postgres_image_id", "firewall_image_id", "firewall_policy_sha256",
        "trusted_entrypoint_sha256", "network_probe_sha256", "attestation_signer_sha256",
        "execution_host", "source_mount_readonly", "scratch_mount_writable",
        "docker_socket_mounted", "host_home_mounted", "host_credentials_mounted",
        "credential_access", "network_mode",
        "egress_allowed", "firewall_defaults_drop", "firewall_ipv4_default_drop",
        "firewall_ipv6_default_drop", "firewall_policy_applied", "candidate_blocked_until_probe",
        "postgres_namespace_egress_blocked", "readonly_fixture_allowlist", "mounts",
        "environment_allowlist", "postgres_data_mount", "resource_limits", "resources",
        "network_probe", "postgres_network_probe", "result", "cleanup",
    }
    predicate = _object(value, keys, "predicate")
    if not isinstance(predicate["bundle_id"], str) or not _BUNDLE_ID.fullmatch(predicate["bundle_id"]):
        raise AttestationError("Gate attestation bundle ID is invalid")
    if type(predicate["pr_number"]) is not int or predicate["pr_number"] <= 0:
        raise AttestationError("Gate attestation PR number is invalid")
    if not isinstance(predicate["target_ref"], str) or not _REF.fullmatch(predicate["target_ref"]):
        raise AttestationError("Gate attestation target ref is invalid")
    for key in ("target_base", "candidate_commit", "candidate_tree"):
        if not isinstance(predicate[key], str) or not _OID.fullmatch(predicate[key]):
            raise AttestationError(f"Gate attestation {key} is invalid")
    for key in ("candidate_archive_sha256", "gate_command_sha256", "gate_policy_sha256"):
        _digest(predicate[key], key)

    argv = predicate["gate_argv"]
    if (not isinstance(argv, list) or not 1 <= len(argv) <= 32
            or sum(len(item) for item in argv if isinstance(item, str)) > 8192
            or any(not isinstance(item, str) or not item or len(item) > 1024
                   or any(ord(char) < 32 for char in item) for item in argv)):
        raise AttestationError("Gate attestation command argv is invalid")
    for key in ("runner_identity", "runner_version", "runner_image_id", "postgres_image_id",
                "firewall_image_id", "execution_host"):
        _text(predicate[key], key)
    if not _IMAGE_ID.fullmatch(predicate["runner_image_id"]):
        raise AttestationError("Runner image must be pinned by immutable SHA-256 ID")
    if not _IMAGE_ID.fullmatch(predicate["postgres_image_id"]):
        raise AttestationError("PostgreSQL image must be pinned by immutable SHA-256 ID")
    if not _IMAGE_ID.fullmatch(predicate["firewall_image_id"]):
        raise AttestationError("Firewall image must be pinned by immutable SHA-256 ID")
    _digest(predicate["trusted_entrypoint_sha256"], "trusted_entrypoint_sha256")
    _digest(predicate["firewall_policy_sha256"], "firewall_policy_sha256")
    _digest(predicate["network_probe_sha256"], "network_probe_sha256")
    _digest(predicate["attestation_signer_sha256"], "attestation_signer_sha256")
    for key in ("source_mount_readonly", "scratch_mount_writable"):
        if type(predicate[key]) is not bool or predicate[key] is not True:
            raise AttestationError(f"Gate attestation {key} is not enabled")
    for key in ("docker_socket_mounted", "host_home_mounted", "host_credentials_mounted",
                "egress_allowed"):
        if type(predicate[key]) is not bool or predicate[key] is not False:
            raise AttestationError(f"Gate attestation {key} violates isolation policy")
    if predicate["credential_access"] != "synthetic_database_only":
        raise AttestationError("Candidate gate received credentials beyond its synthetic database")
    if predicate["network_mode"] != "internal":
        raise AttestationError("Candidate gate network is not isolated")
    for key in ("firewall_defaults_drop", "firewall_ipv4_default_drop",
                "firewall_ipv6_default_drop", "firewall_policy_applied",
                "candidate_blocked_until_probe", "postgres_namespace_egress_blocked"):
        if type(predicate[key]) is not bool or predicate[key] is not True:
            raise AttestationError(f"Gate {key} is not enabled")
    allowlist = predicate["readonly_fixture_allowlist"]
    if (not isinstance(allowlist, list) or len(allowlist) > 64
            or any(not isinstance(item, str) or not item.startswith("/")
                   or ".." in Path(item).parts or len(item) > 512 for item in allowlist)
            or len(set(allowlist)) != len(allowlist)):
        raise AttestationError("Gate fixture allowlist is invalid")
    if allowlist != ["/runner/entrypoint.py", "/runner/network_probe.py"]:
        raise AttestationError("Gate fixture allowlist differs from the reviewed runner entrypoint")

    mounts = predicate["mounts"]
    if not isinstance(mounts, list) or not 2 <= len(mounts) <= 66:
        raise AttestationError("Gate mount inventory is invalid")
    targets = set()
    source_count = scratch_count = 0
    fixture_targets = set()
    for mount in mounts:
        row = _object(mount, {"target", "mode", "kind", "source_class", "source"}, "mount record")
        target = row["target"]
        if (not isinstance(target, str) or not target.startswith("/") or len(target) > 512
                or ".." in Path(target).parts or target in targets):
            raise AttestationError("Gate mount target is invalid or duplicated")
        targets.add(target)
        if row["source_class"] == "candidate_archive":
            source_count += 1
            if (row["mode"] != "ro" or row["kind"] != "bind"
                    or row["source"] != "sha256:" + predicate["candidate_archive_sha256"]):
                raise AttestationError("Candidate archive mount must be read-only")
        elif row["source_class"] == "scratch":
            scratch_count += 1
            if row["mode"] != "rw" or row["kind"] != "tmpfs" or row["source"] != "tmpfs":
                raise AttestationError("Only dedicated scratch tmpfs may be writable")
        elif row["source_class"] == "trusted_runner_fixture":
            expected_fixture_digest = {
                "/runner/entrypoint.py": predicate["trusted_entrypoint_sha256"],
                "/runner/network_probe.py": predicate["network_probe_sha256"],
            }.get(target)
            if (row["mode"] != "ro" or row["kind"] != "bind" or expected_fixture_digest is None
                    or row["source"] != "sha256:" + expected_fixture_digest):
                raise AttestationError("Fixture mounts must be read-only")
            fixture_targets.add(target)
        else:
            raise AttestationError("Gate mounted an unapproved host resource")
    if source_count != 1 or scratch_count != 1 or fixture_targets != set(allowlist):
        raise AttestationError("Gate mount inventory does not match the isolation policy")
    environment = predicate["environment_allowlist"]
    if (not isinstance(environment, list) or len(environment) > 128
            or any(not isinstance(name, str) or not _ENV_NAME.fullmatch(name) for name in environment)
            or len(set(environment)) != len(environment)):
        raise AttestationError("Gate environment allowlist is invalid")

    limits = _object(predicate["resource_limits"],
                     {"cpu_millis", "memory_bytes", "pids", "timeout_seconds"}, "resource limits")
    if any(type(limits[key]) is not int or limits[key] <= 0 for key in limits):
        raise AttestationError("Gate resource limits must be positive integers")
    if (limits["cpu_millis"] > 64_000 or limits["memory_bytes"] > 256 * 1024**3
            or limits["pids"] > 65536 or limits["timeout_seconds"] > 24 * 60 * 60):
        raise AttestationError("Gate resource limits exceed trusted bounds")

    probe = _object(predicate["network_probe"],
                    {"postgres_tcp_allowed", "dns_blocked", "external_ipv4_blocked",
                     "external_ipv6_blocked", "host_gateway_listener_blocked", "host_listener_port",
                     "log_sha256"}, "network probe")
    for key in ("postgres_tcp_allowed", "dns_blocked", "external_ipv4_blocked",
                "external_ipv6_blocked", "host_gateway_listener_blocked"):
        if type(probe[key]) is not bool or probe[key] is not True:
            raise AttestationError("Network isolation probe did not pass")
    if (type(probe["host_listener_port"]) is not int
            or not 1024 <= probe["host_listener_port"] <= 65535
            or probe["host_listener_port"] == 5432):
        raise AttestationError("Network probe host listener port is invalid")
    _digest(probe["log_sha256"], "network_probe.log_sha256")
    postgres_probe = _object(predicate["postgres_network_probe"],
                             {"dns_blocked", "external_ipv4_blocked", "external_ipv6_blocked",
                              "host_gateway_listener_blocked", "host_listener_port",
                              "postgres_unix_socket_ready", "log_sha256"},
                             "PostgreSQL namespace probe")
    for key in ("dns_blocked", "external_ipv4_blocked", "external_ipv6_blocked",
                "host_gateway_listener_blocked", "postgres_unix_socket_ready"):
        if type(postgres_probe[key]) is not bool or postgres_probe[key] is not True:
            raise AttestationError("PostgreSQL namespace isolation probe did not pass")
    if (type(postgres_probe["host_listener_port"]) is not int
            or postgres_probe["host_listener_port"] != probe["host_listener_port"]):
        raise AttestationError("Namespace probes do not bind the same host listener")
    _digest(postgres_probe["log_sha256"], "postgres_network_probe.log_sha256")

    resources = _object(predicate["resources"],
                        {"runner_container_id", "pg_container_id", "network_id", "test_image_id",
                         "postgres_image_id", "firewall_container_id", "probe_container_id",
                         "firewall_image_id", "postgres_firewall_container_id",
                         "postgres_probe_container_id"}, "owned resources")
    for key in ("runner_container_id", "pg_container_id", "network_id", "firewall_container_id",
                "probe_container_id", "postgres_firewall_container_id", "postgres_probe_container_id"):
        if not isinstance(resources[key], str) or not _CONTAINER_ID.fullmatch(resources[key]):
            raise AttestationError(f"Gate {key} must be a full immutable resource ID")
    if not isinstance(resources["test_image_id"], str) or not _IMAGE_ID.fullmatch(resources["test_image_id"]):
        raise AttestationError("Gate test image must be pinned by immutable SHA-256 ID")
    if resources["test_image_id"] != predicate["runner_image_id"]:
        raise AttestationError("Gate runner image differs from the inspected immutable image")
    if resources["postgres_image_id"] != predicate["postgres_image_id"]:
        raise AttestationError("PostgreSQL image must be pinned by immutable SHA-256 ID")
    if resources["firewall_image_id"] != predicate["firewall_image_id"]:
        raise AttestationError("Firewall image must be pinned by immutable SHA-256 ID")
    pg_mount = _object(predicate["postgres_data_mount"],
                       {"type", "source", "size_bytes"}, "PostgreSQL data mount")
    if pg_mount != {"type": "tmpfs", "source": "tmpfs", "size_bytes": 1024**3}:
        raise AttestationError("PostgreSQL data must use a 1 GiB tmpfs mount")

    result = _object(predicate["result"],
                     {"exit_code", "log_sha256", "started_at", "finished_at", "preflight"}, "result")
    if type(result["exit_code"]) is not int or result["exit_code"] != 0:
        raise AttestationError("Full test gate did not pass")
    _digest(result["log_sha256"], "result.log_sha256")
    for key in ("started_at", "finished_at"):
        _text(result[key], f"result.{key}", 64)
        if not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z", result[key]):
            raise AttestationError(f"Gate result {key} is not UTC RFC 3339")
    try:
        started = datetime.fromisoformat(result["started_at"].replace("Z", "+00:00"))
        finished = datetime.fromisoformat(result["finished_at"].replace("Z", "+00:00"))
    except ValueError as error:
        raise AttestationError("Gate result time is invalid") from error
    if finished < started:
        raise AttestationError("Gate result times are reversed")
    preflight = _object(result["preflight"],
                        {"candidate_archive_readonly", "candidate_copied_to_scratch",
                         "imported_package_path", "host_gateway_probe", "external_direct_ip_probe",
                         "external_dns_probe"}, "preflight")
    if (preflight != {"candidate_archive_readonly": True,
                      "candidate_copied_to_scratch": True,
                      "imported_package_path": "/scratch/workspace/src/skybuild/__init__.py",
                      "host_gateway_probe": "blocked", "external_direct_ip_probe": "blocked",
                      "external_dns_probe": "blocked"}):
        raise AttestationError("Gate sandbox preflight did not pass")

    cleanup = _object(predicate["cleanup"], {"status", "owned_resources"}, "cleanup")
    if cleanup["status"] != "confirmed" or not isinstance(cleanup["owned_resources"], list):
        raise AttestationError("Gate cleanup is not confirmed")
    expected_resources = {resources[key] for key in
                          ("runner_container_id", "pg_container_id", "network_id",
                           "firewall_container_id", "probe_container_id",
                           "postgres_firewall_container_id", "postgres_probe_container_id")}
    if len(expected_resources) != 7:
        raise AttestationError("Gate resource IDs are not unique")
    observed_resources = set()
    for item in cleanup["owned_resources"]:
        row = _object(item, {"kind", "id", "state"}, "cleanup resource")
        _text(row["kind"], "cleanup resource kind", 64)
        _text(row["id"], "cleanup resource ID", 512)
        if row["state"] != "absent" or row["id"] in observed_resources:
            raise AttestationError("A gate resource remains or is duplicated")
        observed_resources.add(row["id"])
    if observed_resources != expected_resources:
        raise AttestationError("Gate cleanup did not account for every owned resource")
    return predicate


def _read_private_key(path: Path) -> bytes:
    if not path.is_absolute():
        raise AttestationError("Trusted key path must be absolute")
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size < 32
                or info.st_size > MAX_KEY_BYTES):
            raise AttestationError("Trusted HMAC key must be an owned regular mode-0600 file")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            key = stream.read(MAX_KEY_BYTES + 1)
    except OSError as error:
        raise AttestationError("Trusted HMAC key could not be read safely") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not 32 <= len(key) <= MAX_KEY_BYTES:
        raise AttestationError("Trusted HMAC key size is invalid")
    return key


def verify_attestation(receipt: Any, *, trusted_keys: Mapping[str, Path],
                       expected_predicate: dict, expected_key_id: str) -> dict:
    """Validate schema, strict sandbox claims, pinned fields and host HMAC.

    ``expected_key_id`` is trusted publisher configuration; it is never
    selected from receipt content. All predicate members are exact compared
    against caller-computed bundle, PR, gate and candidate pins.
    """
    value = _object(receipt, {"schema", "predicate", "signature"}, "envelope")
    if len(canonical_bytes(value)) > MAX_ATTESTATION_BYTES:
        raise AttestationError("Gate attestation exceeds size limit")
    if value["schema"] != SCHEMA:
        raise AttestationError("Gate attestation schema is unsupported")
    predicate = _validate_predicate(value["predicate"])
    if not isinstance(expected_predicate, dict) or set(expected_predicate) != PINNED_PREDICATE_FIELDS:
        actual = set(expected_predicate) if isinstance(expected_predicate, dict) else set()
        raise AttestationError("Trusted expected gate pins have missing or unknown fields: "
                               f"missing={sorted(PINNED_PREDICATE_FIELDS - actual)}, "
                               f"unknown={sorted(actual - PINNED_PREDICATE_FIELDS)}")
    if any(predicate.get(key) != expected for key, expected in expected_predicate.items()):
        raise AttestationError("Gate attestation does not match expected publication inputs")
    if not isinstance(expected_key_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", expected_key_id):
        raise AttestationError("Trusted key ID configuration is invalid")
    signature = _object(value["signature"], {"algorithm", "key_id", "value"}, "signature")
    if (signature["algorithm"] != "HMAC-SHA256" or signature["key_id"] != expected_key_id
            or not isinstance(signature["value"], str) or not _SHA256.fullmatch(signature["value"])):
        raise AttestationError("Gate attestation signature metadata is invalid")
    if (not isinstance(trusted_keys, Mapping) or not trusted_keys
            or len(trusted_keys) > 16 or expected_key_id not in trusted_keys
            or any(not isinstance(key_id, str)
                   or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", key_id)
                   or not isinstance(path, Path) or not path.is_absolute()
                   for key_id, path in trusted_keys.items())):
        raise AttestationError("Trusted key-ID map is invalid")
    key = _read_private_key(trusted_keys[expected_key_id])
    actual = hmac.new(key, canonical_bytes(value), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(actual, signature["value"]):
        raise AttestationError("Gate attestation signature verification failed")
    return predicate


def sign_attestation(predicate: dict, *, key_path: Path, key_id: str) -> dict:
    """Sign a validated predicate after the trusted supervisor completes."""
    _validate_predicate(predicate)
    if not isinstance(key_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", key_id):
        raise AttestationError("Trusted key ID configuration is invalid")
    receipt = {"schema": SCHEMA, "predicate": predicate}
    receipt["signature"] = {"algorithm": "HMAC-SHA256", "key_id": key_id, "value": "0" * 64}
    key = _read_private_key(key_path)
    receipt["signature"]["value"] = hmac.new(key, canonical_bytes(receipt), hashlib.sha256).hexdigest()
    return receipt
