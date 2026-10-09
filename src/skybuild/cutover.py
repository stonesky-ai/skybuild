"""Preflight helpers for the one-time live task-authority switch."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from .contracts import DomainError


def verify_backup(path: Path, expected_sha256: str, repository: Path, *,
                  evidence_path: Path, expected_evidence_sha256: str,
                  expected_database: str, expected_system_identifier: str,
                  expected_container_id: str) -> dict:
    """Require a private dump plus pinned source evidence and inspect it inside that cluster."""
    if (not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_evidence_sha256)
            or not expected_system_identifier.isdecimal()
            or not re.fullmatch(r"[0-9a-f]{64}", expected_container_id)):
        raise DomainError("cutover_backup", "Full backup and cluster identity digests are required", 409)
    try:
        backup = path.resolve(strict=True)
        info = backup.stat()
    except OSError as error:
        raise DomainError("cutover_backup", "Cutover backup is unavailable", 409) from error
    root = repository.resolve()
    if (not backup.is_file() or backup.is_relative_to(root) or info.st_size < 16
            or os.name != "nt" and info.st_mode & 0o077):
        raise DomainError("cutover_backup", "Backup must be a private file outside the repository", 409)
    hasher = hashlib.sha256()
    with backup.open("rb") as stream:
        signature = stream.read(5)
        hasher.update(signature)
        for chunk in iter(lambda: stream.read(1_048_576), b""):
            hasher.update(chunk)
    digest = hasher.hexdigest()
    if digest != expected_sha256 or signature != b"PGDMP":
        raise DomainError("cutover_backup", "Backup digest or PostgreSQL archive signature differs", 409)
    try:
        evidence = evidence_path.resolve(strict=True)
        evidence_info = evidence.stat()
        if (not evidence.is_file() or evidence.is_relative_to(root)
                or os.name != "nt" and evidence_info.st_mode & 0o077):
            raise ValueError
        evidence_bytes = evidence.read_bytes()
        if hashlib.sha256(evidence_bytes).hexdigest() != expected_evidence_sha256:
            raise ValueError
        manifest = json.loads(evidence_bytes)
        if (not isinstance(manifest, dict)
                or set(manifest) != {"database", "system_identifier", "container_id", "backup_path", "backup_sha256", "backup_bytes"}
                or manifest["database"] != expected_database
                or manifest["system_identifier"] != expected_system_identifier
                or manifest["container_id"] != expected_container_id
                or manifest["backup_path"] != str(backup)
                or manifest["backup_sha256"] != digest
                or manifest["backup_bytes"] != info.st_size):
            raise ValueError
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise DomainError("cutover_backup", "Pinned backup source evidence is unavailable or mismatched", 409) from error
    try:
        with backup.open("rb") as stream:
            checked = subprocess.run(["docker", "exec", "--interactive", "--user", "postgres",
                                      expected_container_id, "pg_restore", "--list"], stdin=stream,
                                     capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise DomainError("cutover_backup", "Could not inspect the PostgreSQL backup", 409) from error
    archive_text = checked.stdout.decode("utf-8", errors="replace")
    archive_databases = re.findall(r"^;\s+(?:Database|dbname): ([^\r\n]*)\s*$", archive_text, re.MULTILINE)
    if (checked.returncode or not checked.stdout or len(checked.stdout) > 1_048_576
            or archive_databases != [expected_database]):
        raise DomainError("cutover_backup", "PostgreSQL backup archive listing failed", 409)
    return {"path": str(backup), "sha256": digest, "bytes": info.st_size,
            "archive_list_bytes": len(checked.stdout), "evidence_sha256": expected_evidence_sha256,
            "system_identifier": expected_system_identifier, "database": expected_database,
            "container_id": expected_container_id}
