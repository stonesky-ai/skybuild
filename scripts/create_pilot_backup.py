#!/usr/bin/env python3
"""Create a new private pilot dump and source-cluster evidence without overwriting files."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from _repo_guard import verify_skybuild


def _private_new_file(path: Path, repository: Path) -> tuple[Path, int]:
    parent = path.parent.resolve(strict=True)
    resolved = parent / path.name
    info = parent.stat()
    if (resolved.is_relative_to(repository) or info.st_uid != os.geteuid() or info.st_mode & 0o077
            or resolved.exists() or resolved.is_symlink()):
        raise ValueError("Backup outputs require new paths in a private external directory")
    descriptor = os.open(resolved, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.fchmod(descriptor, 0o600)
    return resolved, descriptor


def create_backup(repository: Path, container_id: str, expected_system_identifier: str,
                  backup_path: Path, evidence_path: Path) -> dict:
    if not re.fullmatch(r"[0-9a-f]{64}", container_id):
        raise ValueError("A retained full PostgreSQL container ID is required")
    if not re.fullmatch(r"[0-9]{1,32}", expected_system_identifier):
        raise ValueError("A retained PostgreSQL system identifier is required")
    inspected = json.loads(subprocess.run(["docker", "inspect", container_id], capture_output=True,
                                          text=True, check=True, timeout=10).stdout)[0]
    labels = inspected["Config"]["Labels"]
    if (inspected["Id"] != container_id or not inspected["State"]["Running"]
            or labels.get("com.docker.compose.project") != "skybuild-pilot"
            or labels.get("com.docker.compose.service") != "db"
            or inspected["Config"]["Image"] != "postgres:16"):
        raise ValueError("Retained container is not the running reviewed pilot PostgreSQL container")
    identity = subprocess.run(
        ["docker", "exec", "--user", "postgres", container_id, "psql", "-U", "postgres", "-d",
         "skybuild_pilot", "-Atqc", "SELECT current_database() || ':' || system_identifier::text FROM pg_control_system()"],
        capture_output=True, text=True, check=True, timeout=15).stdout.strip()
    if identity != "skybuild_pilot:" + expected_system_identifier:
        raise ValueError("Pilot container database or PostgreSQL system identity differs from retained preflight")

    backup_path, backup_fd = _private_new_file(backup_path, repository)
    with os.fdopen(backup_fd, "wb") as output:
        subprocess.run(["docker", "exec", "--user", "postgres", container_id, "nice", "-n", "10",
                        "pg_dump", "--format=custom", "--dbname=skybuild_pilot"],
                       stdout=output, check=True, timeout=3600)
        output.flush()
        os.fsync(output.fileno())
    digest = hashlib.sha256()
    with backup_path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1_048_576), b""):
            digest.update(chunk)
    with backup_path.open("rb") as archive:
        listed = subprocess.run(["docker", "exec", "--interactive", "--user", "postgres", container_id,
                                 "pg_restore", "--list"], stdin=archive, capture_output=True,
                                check=False, timeout=30)
    archive_databases = re.findall(r"^;\s+(?:Database|dbname): ([^\r\n]*)\s*$",
                                   listed.stdout.decode("utf-8", errors="replace"), re.MULTILINE)
    if listed.returncode or archive_databases != ["skybuild_pilot"] or len(listed.stdout) > 1_048_576:
        raise ValueError("Fresh PostgreSQL archive did not pass the pilot database listing check")
    manifest = {"database": "skybuild_pilot", "system_identifier": expected_system_identifier,
                "container_id": container_id,
                "backup_path": str(backup_path), "backup_sha256": digest.hexdigest(),
                "backup_bytes": backup_path.stat().st_size}
    evidence_path, evidence_fd = _private_new_file(evidence_path, repository)
    with os.fdopen(evidence_fd, "w", encoding="utf-8") as output:
        output.write(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n")
        output.flush()
        os.fsync(output.fileno())
    evidence_sha = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    return {"backup_file": str(backup_path), "backup_sha256": digest.hexdigest(),
            "backup_bytes": backup_path.stat().st_size, "evidence_file": str(evidence_path),
            "evidence_sha256": evidence_sha, "database": "skybuild_pilot",
            "system_identifier": expected_system_identifier}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--container-id", required=True)
    parser.add_argument("--expected-system-identifier", required=True)
    parser.add_argument("--backup-file", type=Path, required=True)
    parser.add_argument("--evidence-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        repository = verify_skybuild(args.checkout.resolve())
        print(json.dumps(create_backup(repository, args.container_id, args.expected_system_identifier,
                                       args.backup_file, args.evidence_file), sort_keys=True))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(json.dumps({"ok": False, "error": type(error).__name__}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
