"""Recovery requires a dead launcher, physical exit, and resolved publication."""

import json
import socket
import time
from pathlib import Path

import pytest

from scripts import managed_session_state as state
from scripts.managed_session import launcher_identity, private_directory, write_new

UNIT = "skybuild-merge-" + "a" * 32 + ".service"
INVOCATION = "b" * 32


@pytest.fixture
def records(tmp_path, monkeypatch):
    registry = tmp_path / "state"
    private_directory(registry)
    (registry / "admission.lock").touch(mode=0o600)
    launch = {"schema": "skybuild.managed-session.v1", "unit": UNIT, "profile": "merge",
              "host": socket.gethostname(), "target_ref": "refs/heads/dev-006", "launcher": launcher_identity()}
    result = {"unit": UNIT, "invocation_id": INVOCATION, "cleanup_confirmed": True,
              "remaining_process_count": 0, "control_group": "/user.slice/app.slice/"+UNIT,
              "finished_at": time.time()-10}
    evidence = {"schema": "skybuild.merge-resolution.v1", "unit": UNIT,
                "target_ref": "refs/heads/dev-006", "invocation_id": INVOCATION,
                "publication_state": "not-started", "reason": "The command performed a read-only check.",
                "observed_at": time.time()-1}
    write_new(registry / f"{UNIT}.json", launch)
    write_new(registry / f"{UNIT}.result.json", result)
    write_new(registry / "merge-slot.json", {"unit": UNIT, "target_ref": "refs/heads/dev-006"})
    resolution = tmp_path / "resolution.json"
    write_new(resolution, evidence)
    monkeypatch.setattr(state, "checked", lambda *a, **k: "LoadState=not-found\nActiveState=inactive")
    monkeypatch.setattr(state, "launcher_alive", lambda record: False)
    return registry, resolution


def change(path, **changes):
    data = json.loads(path.read_text())
    data.update(changes)
    path.write_text(json.dumps(data))


def test_recovery_is_durable_and_idempotent(records):
    registry, resolution = records
    first = state.recover(registry, UNIT, resolution, {})
    assert not (registry / "merge-slot.json").exists()
    assert first["publication_state"] == "not-started"
    assert first["invocation_id"] == INVOCATION
    assert state.recover(registry, UNIT, resolution, {}) == first
    assert len(list((registry / "recoveries").glob("*.json"))) == 1


@pytest.mark.parametrize("publication_state", ["pending", "unknown", None])
def test_unresolved_publication_keeps_slot(records, publication_state):
    registry, resolution = records
    change(resolution, publication_state=publication_state)
    with pytest.raises(state.SessionError, match="Publication is not resolved"):
        state.recover(registry, UNIT, resolution, {})
    assert (registry / "merge-slot.json").is_file()


@pytest.mark.parametrize("field,value", [("unit", "different.service"), ("target_ref", "refs/heads/dev-005"),
                                         ("invocation_id", "c" * 32), ("observed_at", 0)])
def test_resolution_must_match_service_and_completion(records, field, value):
    registry, resolution = records
    change(resolution, **{field: value})
    with pytest.raises(state.SessionError, match="Publication is not resolved"):
        state.recover(registry, UNIT, resolution, {})
    assert (registry / "merge-slot.json").is_file()


def test_live_launcher_prevents_recovery(records, monkeypatch):
    registry, resolution = records
    monkeypatch.setattr(state, "launcher_alive", lambda record: True)
    with pytest.raises(state.SessionError, match="launcher is alive"):
        state.recover(registry, UNIT, resolution, {})
    assert (registry / "merge-slot.json").is_file()


def test_live_service_prevents_recovery(records, monkeypatch):
    registry, resolution = records
    monkeypatch.setattr(state, "checked", lambda *a, **k:
                        f"LoadState=loaded\nActiveState=active\nInvocationID={INVOCATION}")
    with pytest.raises(state.SessionError, match="exit and cleanup"):
        state.recover(registry, UNIT, resolution, {})


def test_replacement_invocation_prevents_recovery(records, monkeypatch):
    registry, resolution = records
    monkeypatch.setattr(state, "checked", lambda *a, **k:
                        "LoadState=loaded\nActiveState=failed\nInvocationID=" + "c" * 32)
    with pytest.raises(state.SessionError, match="exit and cleanup"):
        state.recover(registry, UNIT, resolution, {})


def test_missing_cleanup_prevents_recovery(records):
    registry, resolution = records
    (registry / f"{UNIT}.result.json").unlink()
    with pytest.raises(state.SessionError, match="exit and cleanup"):
        state.recover(registry, UNIT, resolution, {})


def test_other_slot_owner_is_preserved(records):
    registry, resolution = records
    other = "skybuild-merge-" + "d" * 32 + ".service"
    change(registry / "merge-slot.json", unit=other)
    with pytest.raises(state.SessionError, match="different owner"):
        state.recover(registry, UNIT, resolution, {})
    assert json.loads((registry / "merge-slot.json").read_text())["unit"] == other
    assert not list((registry / "recoveries").glob("*.json"))


def test_current_launcher_identity_is_live():
    # Use the real /proc identity; no process is signaled in this test.
    assert state.launcher_alive({"launcher": launcher_identity()}) is True


def test_status_does_not_create_absent_state(tmp_path):
    registry = tmp_path / "absent"
    report = state.status(registry, {})
    assert report["state_available"] is False
    assert not registry.exists()


def test_status_reports_service_without_releasing_slot(records):
    registry, _ = records
    report = state.status(registry, {})
    assert report["services"][0]["physical_exit_confirmed"] is True
    assert report["merge_slot"]["unit"] == UNIT
    assert (registry / "merge-slot.json").is_file()
