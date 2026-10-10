"""HMAC receipts for the trusted isolated full-test gate supervisor."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat


SCHEMA = "skybuild.isolated-full-test-attestation.v1"
PINNED_PREDICATE_FIELDS = frozenset({
    "bundle_id", "pr_number", "target_ref", "target_base", "candidate_commit", "candidate_tree",
    "candidate_archive_sha256", "candidate_history_sha256", "gate_argv", "gate_command_sha256",
    "gate_policy_sha256", "runner_identity", "runner_version", "runner_image_id",
    "postgres_image_id", "firewall_image_id", "firewall_policy_sha256", "network_probe_sha256",
    "trusted_entrypoint_sha256", "attestation_signer_sha256", "candidate_blocked_until_probe",
    "execution_host",
})
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class AttestationError(ValueError):
    """The receipt or signer key does not satisfy the trusted gate contract."""


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                          allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise AttestationError("Attestation payload is not canonical JSON") from error


def _key(path: Path) -> bytes:
    path = Path(path)
    try:
        info = path.lstat()
    except OSError as error:
        raise AttestationError("Attestation key is unavailable") from error
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or not path.is_absolute()):
        raise AttestationError("Attestation key must be an owned private regular file")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (not stat.S_ISREG(opened.st_mode) or opened.st_ino != info.st_ino
                or opened.st_dev != info.st_dev or opened.st_size > 4096):
            raise AttestationError("Attestation key changed or exceeds its size bound")
        material = stream.read(4097)
    if not 32 <= len(material) <= 4096:
        raise AttestationError("Attestation key length is invalid")
    return material


def _signature(payload: dict, material: bytes) -> str:
    return hmac.new(material, SCHEMA.encode("utf-8") + b"\0" + _canonical(payload),
                    hashlib.sha256).hexdigest()


def sign_attestation(predicate: dict, *, key_path: Path, key_id: str) -> dict:
    if not isinstance(predicate, dict) or not PINNED_PREDICATE_FIELDS <= set(predicate):
        raise AttestationError("Gate receipt omits pinned candidate and runner identity")
    if not isinstance(key_id, str) or not key_id or len(key_id) > 128 or "\0" in key_id:
        raise AttestationError("Attestation key ID is invalid")
    material = _key(Path(key_path))
    return {"schema": SCHEMA, "key_id": key_id, "predicate": predicate,
            "signature": _signature(predicate, material)}


def verify_attestation(receipt: object, *, trusted_keys: dict[str, Path],
                       expected_predicate: dict | None = None,
                       expected_key_id: str | None = None) -> dict:
    if (not isinstance(receipt, dict)
            or set(receipt) != {"schema", "key_id", "predicate", "signature"}
            or receipt.get("schema") != SCHEMA
            or not isinstance(receipt.get("key_id"), str)
            or not isinstance(receipt.get("predicate"), dict)
            or not isinstance(receipt.get("signature"), str)
            or not _SHA256.fullmatch(receipt["signature"])
            or not isinstance(trusted_keys, dict)):
        raise AttestationError("Attestation envelope has missing or unknown fields")
    key_id = receipt["key_id"]
    if expected_key_id is not None and key_id != expected_key_id:
        raise AttestationError("Attestation key ID differs from the trusted pin")
    key_path = trusted_keys.get(key_id)
    if not isinstance(key_path, Path):
        raise AttestationError("Attestation key ID is not trusted")
    material = _key(key_path)
    expected_signature = _signature(receipt["predicate"], material)
    if not hmac.compare_digest(expected_signature, receipt["signature"]):
        raise AttestationError("Attestation signature does not verify")
    if not PINNED_PREDICATE_FIELDS <= set(receipt["predicate"]):
        raise AttestationError("Attestation omits pinned candidate and runner identity")
    if expected_predicate is not None:
        if (not isinstance(expected_predicate, dict)
                or any(receipt["predicate"].get(name) != value
                       for name, value in expected_predicate.items())):
            raise AttestationError("Attestation differs from the expected frozen predicate")
    return receipt["predicate"]
