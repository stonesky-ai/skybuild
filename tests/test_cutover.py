import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from skybuild.contracts import DomainError
from skybuild.cutover import verify_backup

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from create_pilot_backup import create_backup


@pytest.mark.parametrize("archive_line", [b";     Database: skybuild_pilot\n", b";     dbname: skybuild_pilot\n"])
def test_backup_preflight_requires_private_pinned_postgres_archive(tmp_path, monkeypatch, archive_line):
    repository = tmp_path / "repo"
    repository.mkdir()
    backup = tmp_path / "pilot.dump"
    payload = b"PGDMP" + b"disposable archive fixture"
    backup.write_bytes(payload)
    backup.chmod(0o600)
    evidence = tmp_path / "pilot-backup.json"
    manifest = {"database": "skybuild_pilot", "system_identifier": "123456789", "container_id": "a" * 64,
                "backup_path": str(backup.resolve()), "backup_sha256": hashlib.sha256(payload).hexdigest(),
                "backup_bytes": len(payload)}
    evidence.write_text(json.dumps(manifest))
    evidence.chmod(0o600)
    monkeypatch.setattr("skybuild.cutover.subprocess.run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout=archive_line))
    evidence_sha = hashlib.sha256(evidence.read_bytes()).hexdigest()
    report = verify_backup(backup, hashlib.sha256(payload).hexdigest(), repository,
                           evidence_path=evidence, expected_evidence_sha256=evidence_sha,
                           expected_database="skybuild_pilot", expected_system_identifier="123456789",
                           expected_container_id="a" * 64)
    assert report["sha256"] == hashlib.sha256(payload).hexdigest()
    assert report["bytes"] == len(payload)
    assert report["system_identifier"] == "123456789"


def test_backup_preflight_refuses_repo_files_and_wrong_hash(tmp_path, monkeypatch):
    repository = tmp_path / "repo"
    repository.mkdir()
    backup = repository / "pilot.dump"
    backup.write_bytes(b"PGDMP" + b"external archive fixture")
    backup.chmod(0o600)
    with pytest.raises(DomainError, match="outside the repository"):
        verify_backup(backup, hashlib.sha256(backup.read_bytes()).hexdigest(), repository,
                      evidence_path=backup, expected_evidence_sha256="0" * 64,
                      expected_database="skybuild_pilot", expected_system_identifier="123456789",
                      expected_container_id="a" * 64)
    external = tmp_path / "external.dump"
    external.write_bytes(b"PGDMP" + b"external archive fixture")
    external.chmod(0o600)
    with pytest.raises(DomainError, match="digest"):
        verify_backup(external, "0" * 64, repository, evidence_path=external,
                      expected_evidence_sha256="0" * 64, expected_database="skybuild_pilot",
                      expected_system_identifier="123456789", expected_container_id="a" * 64)


def test_backup_preflight_refuses_evidence_from_another_cluster(tmp_path, monkeypatch):
    repository = tmp_path / "repo"
    repository.mkdir()
    backup = tmp_path / "pilot.dump"
    payload = b"PGDMP" + b"valid archive fixture"
    backup.write_bytes(payload)
    backup.chmod(0o600)
    evidence = tmp_path / "pilot-backup.json"
    evidence.write_text(json.dumps({"database": "skybuild_pilot", "system_identifier": "wrong-cluster",
                                    "container_id": "a" * 64,
                                    "backup_path": str(backup.resolve()),
                                    "backup_sha256": hashlib.sha256(payload).hexdigest(),
                                    "backup_bytes": len(payload)}))
    evidence.chmod(0o600)
    monkeypatch.setattr("skybuild.cutover.subprocess.run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout=b";     Database: skybuild_pilot\n"))
    with pytest.raises(DomainError, match="source evidence"):
        verify_backup(backup, hashlib.sha256(payload).hexdigest(), repository,
                      evidence_path=evidence, expected_evidence_sha256=hashlib.sha256(evidence.read_bytes()).hexdigest(),
                      expected_database="skybuild_pilot", expected_system_identifier="123456789",
                      expected_container_id="a" * 64)


def test_backup_preflight_refuses_manifest_for_another_database_or_backup(tmp_path, monkeypatch):
    repository = tmp_path / "repo"
    repository.mkdir()
    backup = tmp_path / "pilot.dump"
    payload = b"PGDMP" + b"valid archive fixture"
    backup.write_bytes(payload)
    backup.chmod(0o600)
    evidence = tmp_path / "pilot-backup.json"
    evidence.write_text(json.dumps({"database": "other_database", "system_identifier": "123456789",
                                    "container_id": "a" * 64,
                                    "backup_path": str(backup.resolve()),
                                    "backup_sha256": hashlib.sha256(payload).hexdigest(),
                                    "backup_bytes": len(payload)}))
    evidence.chmod(0o600)
    monkeypatch.setattr("skybuild.cutover.subprocess.run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout=b";     Database: skybuild_pilot\n"))
    with pytest.raises(DomainError, match="source evidence"):
        verify_backup(backup, hashlib.sha256(payload).hexdigest(), repository,
                      evidence_path=evidence, expected_evidence_sha256=hashlib.sha256(evidence.read_bytes()).hexdigest(),
                      expected_database="skybuild_pilot", expected_system_identifier="123456789",
                      expected_container_id="a" * 64)


def test_backup_preflight_refuses_manifest_for_another_container(tmp_path, monkeypatch):
    repository = tmp_path / "repo"
    repository.mkdir()
    backup = tmp_path / "pilot.dump"
    payload = b"PGDMP" + b"valid archive fixture"
    backup.write_bytes(payload)
    backup.chmod(0o600)
    evidence = tmp_path / "pilot-backup.json"
    evidence.write_text(json.dumps({"database": "skybuild_pilot", "system_identifier": "123456789",
                                    "container_id": "b" * 64,
                                    "backup_path": str(backup.resolve()),
                                    "backup_sha256": hashlib.sha256(payload).hexdigest(),
                                    "backup_bytes": len(payload)}))
    evidence.chmod(0o600)
    with pytest.raises(DomainError, match="source evidence"):
        verify_backup(backup, hashlib.sha256(payload).hexdigest(), repository,
                      evidence_path=evidence, expected_evidence_sha256=hashlib.sha256(evidence.read_bytes()).hexdigest(),
                      expected_database="skybuild_pilot", expected_system_identifier="123456789",
                      expected_container_id="a" * 64)


def test_backup_preflight_requires_exact_archive_database_marker(tmp_path, monkeypatch):
    repository = tmp_path / "repo"
    repository.mkdir()
    backup = tmp_path / "pilot.dump"
    payload = b"PGDMP" + b"valid archive fixture"
    backup.write_bytes(payload)
    backup.chmod(0o600)
    evidence = tmp_path / "pilot-backup.json"
    evidence.write_text(json.dumps({"database": "skybuild_pilot", "system_identifier": "123456789",
                                    "container_id": "a" * 64,
                                    "backup_path": str(backup.resolve()),
                                    "backup_sha256": hashlib.sha256(payload).hexdigest(),
                                    "backup_bytes": len(payload)}))
    evidence.chmod(0o600)
    monkeypatch.setattr("skybuild.cutover.subprocess.run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout=b";     Database: skybuild_pilot_evil\n"))
    with pytest.raises(DomainError, match="archive listing"):
        verify_backup(backup, hashlib.sha256(payload).hexdigest(), repository,
                      evidence_path=evidence, expected_evidence_sha256=hashlib.sha256(evidence.read_bytes()).hexdigest(),
                      expected_database="skybuild_pilot", expected_system_identifier="123456789",
                      expected_container_id="a" * 64)


def test_backup_creation_binds_dump_to_retained_container_and_cluster(tmp_path, monkeypatch):
    repository = tmp_path / "repo"
    repository.mkdir()
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    container = "a" * 64

    def run(args, **kwargs):
        if args[:2] == ["docker", "inspect"]:
            inspected = {"Id": container, "State": {"Running": True},
                         "Config": {"Labels": {"com.docker.compose.project": "skybuild-pilot",
                                                "com.docker.compose.service": "db"},
                                    "Image": "postgres:16"}}
            return SimpleNamespace(stdout=json.dumps([inspected]))
        if args[0:3] == ["docker", "exec", "--user"] and "psql" in args:
            assert container in args
            return SimpleNamespace(stdout="skybuild_pilot:123456789\n")
        if args[0:3] == ["docker", "exec", "--user"] and "pg_dump" in args:
            assert container in args
            kwargs["stdout"].write(b"PGDMP" + b"custom archive")
            return SimpleNamespace(returncode=0)
        if args[0:2] == ["docker", "exec"] and "pg_restore" in args:
            assert container in args
            return SimpleNamespace(returncode=0, stdout=b";     dbname: skybuild_pilot\n")
        raise AssertionError("unexpected subprocess")

    monkeypatch.setattr("create_pilot_backup.subprocess.run", run)
    result = create_backup(repository, container, "123456789", private / "pilot.dump", private / "pilot.json")
    assert result["database"] == "skybuild_pilot"
    manifest = json.loads((private / "pilot.json").read_text())
    assert manifest["system_identifier"] == "123456789" and manifest["container_id"] == container
    assert manifest["backup_sha256"] == hashlib.sha256((private / "pilot.dump").read_bytes()).hexdigest()
    assert (private / "pilot.dump").stat().st_mode & 0o777 == 0o600
    assert (private / "pilot.json").stat().st_mode & 0o777 == 0o600


def test_backup_creation_rejects_archive_marker_with_database_prefix(tmp_path, monkeypatch):
    repository = tmp_path / "repo"
    repository.mkdir()
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    container = "a" * 64

    def run(args, **kwargs):
        if args[:2] == ["docker", "inspect"]:
            inspected = {"Id": container, "State": {"Running": True},
                         "Config": {"Labels": {"com.docker.compose.project": "skybuild-pilot",
                                                "com.docker.compose.service": "db"},
                                    "Image": "postgres:16"}}
            return SimpleNamespace(stdout=json.dumps([inspected]))
        if args[0:3] == ["docker", "exec", "--user"] and "psql" in args:
            assert container in args
            return SimpleNamespace(stdout="skybuild_pilot:123456789\n")
        if args[0:3] == ["docker", "exec", "--user"] and "pg_dump" in args:
            assert container in args
            kwargs["stdout"].write(b"PGDMP" + b"custom archive")
            return SimpleNamespace(returncode=0)
        if args[0:2] == ["docker", "exec"] and "pg_restore" in args:
            assert container in args
            return SimpleNamespace(returncode=0, stdout=b";     Database: skybuild_pilot_evil\n")
        raise AssertionError("unexpected subprocess")

    monkeypatch.setattr("create_pilot_backup.subprocess.run", run)
    with pytest.raises(ValueError, match="database listing check"):
        create_backup(repository, container, "123456789", private / "pilot.dump", private / "pilot.json")
    assert not (private / "pilot.json").exists()
