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


def _private_journal(tmp_path):
    private = tmp_path / "journal-private"
    private.mkdir(mode=0o700, parents=True)
    os.chmod(private, 0o700)
    return rehearsal.Journal(private / "run.jsonl", {})


def test_writer_probe_accepts_only_explicit_absence_and_fails_closed(monkeypatch, tmp_path):
    journal = _private_journal(tmp_path)
    monkeypatch.setattr(rehearsal, "_docker", lambda *_args, **_kwargs:
                        rehearsal.subprocess.CompletedProcess([], 1, "", "Error: No such object: rehearsal"))
    rehearsal._check_writers(["rehearsal"], expected=None, journal=journal)
    journal.close()

    monkeypatch.setattr(rehearsal, "_docker", lambda *_args, **_kwargs:
                        rehearsal.subprocess.CompletedProcess([], 1, "", "Cannot connect to Docker daemon"))
    journal = _private_journal(tmp_path / "second")
    with pytest.raises(rehearsal.PreparationError, match="could not establish"):
        rehearsal._check_writers(["rehearsal"], expected=None, journal=journal)
    journal.close()


def test_writer_probe_requires_the_expected_container_id(monkeypatch, tmp_path):
    journal = _private_journal(tmp_path)
    row = {"Name": "/rehearsal", "Id": "a" * 64, "State": {"Running": True}}
    monkeypatch.setattr(rehearsal, "_docker", lambda *_args, **_kwargs:
                        rehearsal.subprocess.CompletedProcess([], 0, json.dumps([row]), ""))
    rehearsal._check_writers(["rehearsal"], expected="rehearsal", expected_id="a" * 64,
                             journal=journal)
    with pytest.raises(rehearsal.PreparationError, match="One-writer invariant"):
        rehearsal._check_writers(["rehearsal"], expected="rehearsal", expected_id="b" * 64,
                                 journal=journal)
    journal.close()


def test_execution_host_is_exact_and_bound(monkeypatch):
    monkeypatch.setattr(rehearsal.platform, "node", lambda: "Wonko.example.test")
    rehearsal._require_execution_host("wonko")
    with pytest.raises(rehearsal.PreparationError, match="does not match"):
        rehearsal._require_execution_host("jeltz")
    with pytest.raises(rehearsal.PreparationError, match="does not match"):
        rehearsal._require_execution_host("unknown")


def test_all_rehearsal_container_runs_are_no_pull_and_resource_limited():
    for profile in ("postgres", "api", "probe"):
        prefix = rehearsal._docker_run_prefix(profile, "--rm")
        assert prefix[:3] == ["run", "--pull=never", *rehearsal.RESOURCE_LIMITS[profile][:1]]
        assert "--cpus=" in prefix[3]
        assert "--pids-limit=" in prefix[4]
    command = rehearsal._candidate_build_command("candidate:run", Path("Dockerfile"), Path("context"))
    assert "--pull=false" in command
    assert "--network=none" in command
    assert f"--memory={rehearsal.BUILD_MEMORY}" in command
    assert f"--cpu-quota={rehearsal.BUILD_CPU_QUOTA}" in command
    assert rehearsal.BUILD_TIMEOUT_SECONDS > 0


def test_psql_stdin_wiring_uses_interactive_exec(monkeypatch):
    calls = []

    def capture(*args, **kwargs):
        calls.append((args, kwargs))
        return rehearsal.subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(rehearsal, "_docker", capture)
    rehearsal._psql_input("pg", "db", "CREATE SCHEMA skybuild;")
    args, kwargs = calls[0]
    assert args[:3] == ("exec", "-i", "pg")
    assert kwargs["input_text"] == "CREATE SCHEMA skybuild;"


def test_candidate_image_cleanup_removes_only_owned_full_image_id(monkeypatch, tmp_path):
    journal = _private_journal(tmp_path)
    image_id = "sha256:" + "a" * 64
    reference = "skybuild-mvp-rehearsal:run"
    labels = {"skybuild.rehearsal.run-id": "run", "skybuild.rehearsal.source": "b" * 40}
    calls = []

    def fake_docker(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("image", "inspect") and args[-1] == reference:
            if any(call[:3] == ("image", "rm", image_id) for call in calls):
                return rehearsal.subprocess.CompletedProcess(args, 1, "", f"Error: No such object: {reference}")
            return rehearsal.subprocess.CompletedProcess(args, 0, f"{image_id} {json.dumps(labels)}", "")
        if args[:2] == ("image", "rm"):
            return rehearsal.subprocess.CompletedProcess(args, 0, image_id, "")
        pytest.fail(f"unexpected Docker call: {args}")

    monkeypatch.setattr(rehearsal, "_docker", fake_docker)
    rehearsal._cleanup_candidate_image(reference, "run", "b" * 40, journal, image_id)
    assert ("image", "rm", image_id) in calls
    journal.close()


def test_candidate_image_cleanup_refuses_unowned_image(monkeypatch, tmp_path):
    journal = _private_journal(tmp_path)
    image_id = "sha256:" + "a" * 64
    labels = {"skybuild.rehearsal.run-id": "other", "skybuild.rehearsal.source": "b" * 40}
    calls = []

    def fake_docker(*args, **kwargs):
        calls.append(args)
        return rehearsal.subprocess.CompletedProcess(args, 0, f"{image_id} {json.dumps(labels)}", "")

    monkeypatch.setattr(rehearsal, "_docker", fake_docker)
    with pytest.raises(rehearsal.PreparationError, match="not owned"):
        rehearsal._cleanup_candidate_image("candidate:run", "run", "b" * 40, journal)
    assert not any(call[:2] == ("image", "rm") for call in calls)
    journal.close()


@pytest.mark.parametrize("kind", ["network", "volume"])
def test_cleanup_reconciles_create_success_when_creation_inspection_failed(monkeypatch, tmp_path, kind):
    journal = _private_journal(tmp_path)
    run_id = "run"
    name = kind + "-run"
    object_id = "a" * 64
    state = {"exists": True, "removed": False}
    calls = []

    def fake_docker(*args, **kwargs):
        calls.append(args)
        if kind == "network" and args[:2] == ("network", "inspect"):
            if not state["exists"]:
                return rehearsal.subprocess.CompletedProcess(args, 1, "", f"Error: No such network: {name}")
            return rehearsal.subprocess.CompletedProcess(args, 0, f"{object_id} {run_id} true", "")
        if kind == "volume" and args[:2] == ("volume", "inspect"):
            if not state["exists"]:
                return rehearsal.subprocess.CompletedProcess(args, 1, "", f"Error: No such volume: {name}")
            return rehearsal.subprocess.CompletedProcess(args, 0, f"{name} {run_id}", "")
        if args[:2] == (kind, "rm"):
            state["exists"] = False
            state["removed"] = True
            return rehearsal.subprocess.CompletedProcess(args, 0, name, "")
        pytest.fail(f"unexpected Docker call: {args}")

    monkeypatch.setattr(rehearsal, "_docker", fake_docker)
    # This is the cleanup path after create returned but its first inspect failed.
    if kind == "network":
        rehearsal._cleanup_network(name, run_id, None, journal)
    else:
        rehearsal._cleanup_volume(name, run_id, True, journal)
    assert state["removed"]
    assert any(call[:2] == (kind, "rm") for call in calls)
    journal.close()


def test_container_create_timeout_reconciles_by_name_and_refuses_foreign_container(monkeypatch, tmp_path):
    journal = _private_journal(tmp_path)
    name, run_id = "api-run", "run"
    container_id = "b" * 64
    state = {"label": run_id, "exists": True}
    calls = []

    def fake_docker(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("run", "--pull=never"):
            raise rehearsal.subprocess.TimeoutExpired(["docker", "run"], 20)
        if args[:3] == ("inspect", "--type", "container"):
            target = args[-1]
            if not state["exists"]:
                return rehearsal.subprocess.CompletedProcess(args, 1, "", f"Error: No such object: {target}")
            if target not in {name, container_id}:
                pytest.fail(f"unexpected inspection target: {target}")
            row = {"Id": container_id, "Name": "/" + name,
                   "Config": {"Labels": {"skybuild.rehearsal.run-id": state["label"]}},
                   "State": {"Running": False}}
            return rehearsal.subprocess.CompletedProcess(args, 0, json.dumps([row]), "")
        if args[:2] == ("rm", "--force"):
            state["exists"] = False
            return rehearsal.subprocess.CompletedProcess(args, 0, container_id, "")
        pytest.fail(f"unexpected Docker call: {args}")

    monkeypatch.setattr(rehearsal, "_docker", fake_docker)
    attempted, created = set(), {}
    with pytest.raises(rehearsal.subprocess.TimeoutExpired):
        rehearsal._create_rehearsal_container(name, run_id, "api", ("image",), 20,
                                              attempted, created, journal)
    assert name in attempted and name not in created
    rehearsal._cleanup_container(name, run_id, None, journal)
    assert not state["exists"]

    state.update(label="foreign", exists=True)
    with pytest.raises(rehearsal.PreparationError, match="ownership or full ID"):
        rehearsal._cleanup_container(name, run_id, None, journal)
    assert state["exists"]
    assert not any(call[:2] == ("rm", "--force") and call[-1] != container_id for call in calls)
    journal.close()


def test_container_id_is_journaled_before_post_create_inspection(monkeypatch, tmp_path):
    journal = _private_journal(tmp_path)
    name, run_id, container_id = "api-run", "run", "c" * 64
    monkeypatch.setattr(rehearsal, "_docker", lambda *args, **kwargs:
                        rehearsal.subprocess.CompletedProcess(args, 0, container_id, ""))
    monkeypatch.setattr(rehearsal, "_inspect_container",
                        lambda _name: (_ for _ in ()).throw(TimeoutError("inspect timeout")))
    attempted, created = set(), {}
    with pytest.raises(TimeoutError):
        rehearsal._create_rehearsal_container(name, run_id, "api", ("image",), 20,
                                              attempted, created, journal)
    assert attempted == {name}
    assert created == {name: container_id}
    journal.close()


@pytest.mark.parametrize("kind", ["network", "volume"])
def test_cleanup_confirms_absence_after_remove_timeout(monkeypatch, tmp_path, kind):
    journal = _private_journal(tmp_path)
    run_id, name, object_id = "run", kind + "-run", "d" * 64
    state = {"exists": True}

    def fake_docker(*args, **kwargs):
        if kind == "network" and args[:2] == ("network", "inspect"):
            if not state["exists"]:
                return rehearsal.subprocess.CompletedProcess(args, 1, "", f"Error: No such network: {name}")
            return rehearsal.subprocess.CompletedProcess(args, 0, f"{object_id} {run_id} true", "")
        if kind == "volume" and args[:2] == ("volume", "inspect"):
            if not state["exists"]:
                return rehearsal.subprocess.CompletedProcess(args, 1, "", f"Error: No such volume: {name}")
            return rehearsal.subprocess.CompletedProcess(args, 0, f"{name} {run_id}", "")
        if args[:2] == (kind, "rm"):
            state["exists"] = False
            raise rehearsal.subprocess.TimeoutExpired(["docker", kind, "rm"], 10)
        pytest.fail(f"unexpected Docker call: {args}")

    monkeypatch.setattr(rehearsal, "_docker", fake_docker)
    if kind == "network":
        rehearsal._cleanup_network(name, run_id, object_id, journal)
    else:
        rehearsal._cleanup_volume(name, run_id, True, journal)
    assert not state["exists"]
    journal.close()
