"""Pure preparation and evidence tests; these never invoke Docker or contact services."""
import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import controller_update_rehearsal as rehearsal  # noqa: E402


def test_evidence_schema_requires_complete_ordered_migration_prefix():
    rows = [f"{version}:{hashlib.sha256(str(version).encode()).hexdigest()}"
            for version in range(1, 14)]
    parsed = rehearsal._evidence_schema({"schema": rows})
    assert [row["version"] for row in parsed] == list(range(1, 14))
    with pytest.raises(rehearsal.PreparationError, match="001–013"):
        rehearsal._evidence_schema({"schema": rows[:-1]})
    with pytest.raises(rehearsal.PreparationError, match="malformed"):
        rehearsal._evidence_schema({"schema": rows[:12] + ["13:not-a-digest"]})


def test_schema_manifest_is_ordered_and_checks_exact_001_to_013(monkeypatch, tmp_path):
    names = [f"src/skybuild/migrations/{version:03}_migration.sql" for version in reversed(range(1, 14))]
    monkeypatch.setattr(rehearsal, "_git", lambda *_args: "\n".join(names))
    monkeypatch.setattr(rehearsal, "_blob", lambda _checkout, _revision, path: path.encode())
    rows = rehearsal.schema_manifest(tmp_path, "a" * 40)
    assert [row["version"] for row in rows] == list(range(1, 14))
    assert rows[0]["sha256"] == hashlib.sha256(b"src/skybuild/migrations/001_migration.sql").hexdigest()
    monkeypatch.setattr(rehearsal, "_git", lambda *_args: "\n".join(names[:-1]))
    with pytest.raises(rehearsal.PreparationError, match="complete ordered migration set"):
        rehearsal.schema_manifest(tmp_path, "a" * 40)


def test_journal_is_private_append_only_and_exclusive(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    os.chmod(private, 0o700)
    path = private / "journal.jsonl"
    journal = rehearsal.Journal(path, {"task_id": "SKYBUILD-MVP-ISOLATED-CONTROLLER-REHEARSAL"})
    journal.event("source_pin", source=rehearsal.CANDIDATE_SOURCE)
    journal.close()
    assert path.stat().st_mode & 0o777 == 0o600
    rows = [json.loads(row) for row in path.read_text().splitlines()]
    assert [row["event"] for row in rows] == ["run_started", "source_pin"]
    with pytest.raises(rehearsal.PreparationError, match="already exists"):
        rehearsal.Journal(path, {})


def test_runtime_go_record_is_required_before_docker(monkeypatch, tmp_path):
    go = {"decision": "NO", "task_id": "SKYBUILD-MVP-ISOLATED-CONTROLLER-REHEARSAL"}
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    os.chmod(private, 0o700)
    path = private / "go.json"
    path.write_text(json.dumps(go))
    path.chmod(0o600)
    evidence_path = tmp_path / "runtime-audit.json"
    evidence_path.write_text(json.dumps({"containers": [
        {"name": "/skybuild-pilot-api", "id": "a" * 64, "image": rehearsal.ACCEPTED_IMAGE},
        {"name": "/skybuild-pilot-pg", "id": "b" * 64, "image": "sha256:" + "c" * 64},
    ]}))
    monkeypatch.setattr(rehearsal, "prepare", lambda *_args: {
        "task_id": go["task_id"], "accepted_controller": {"image": rehearsal.ACCEPTED_IMAGE},
        "candidate": {"source": rehearsal.CANDIDATE_SOURCE, "tree": rehearsal.CANDIDATE_TREE}})
    monkeypatch.setattr(rehearsal, "_docker", lambda *_args, **_kwargs: pytest.fail("Docker must not run"))
    with pytest.raises(rehearsal.PreparationError, match="matching independent-review and root GO"):
        rehearsal.execute(tmp_path, evidence_path, path, tmp_path / "out.jsonl")
