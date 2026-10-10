"""Focused safety tests for the local attempt closeout reducer and journal."""
import hashlib
import json
import re
import stat
from collections import Counter
from pathlib import Path

import pytest

from skybuild.attempt_closeout import (
    AttemptCloseoutJournal, AttemptPin, ClockObservation, CloseoutError,
)


def pin(**changes):
    values = dict(project_id="skybuild", task_id="task-1", attempt_id="attempt-1",
                  claim_fence=7, source_head="a" * 40,
                  input_digest=hashlib.sha256(b"input").hexdigest(), approval_id="approval-1",
                  cutoff_epoch=1000, grace_seconds=300,
                  invocation_id="invocation-1", policy_version="closeout-v1")
    values.update(changes)
    return AttemptPin(**values)


def test_tla_boolean_next_assignments_are_parenthesized():
    model = Path(__file__).parents[1] / "docs/design/models/AttemptCloseout.tla"
    assignments = re.findall(
        r"^\s*(?:/\\\s*)?(badCall(?:Window|Parked|Clock))'\s*=\s*(.+)$",
        model.read_text(), re.MULTILINE,
    )
    assert Counter(name for name, _ in assignments) == {
        "badCallWindow": 3, "badCallParked": 3, "badCallClock": 3,
    }
    assert all(rhs.startswith("(") and rhs.endswith(")") for _, rhs in assignments)


def clock(epoch, boot_id="boot-1"):
    return ClockObservation(boot_id, epoch, epoch * 1_000_000_000,
                            epoch * 1_000_000_000)


@pytest.fixture
def journal(tmp_path):
    directory = tmp_path / "attempt"
    directory.mkdir(mode=0o700)
    return AttemptCloseoutJournal(directory, pin())


def test_cutoff_is_immutable_and_grace_only_allows_bounded_closeout(journal):
    assert pin().outage_policy == "finish-current-task"
    assert pin(source_head="b" * 64).source_head == "b" * 64
    journal.append("running", "process_observed", 990,
                   {"invocation_id": "invocation-1", "state": "running"})
    assert journal.may_admit_new_task(clock(999))
    assert journal.may_call_model(clock(999), purpose="task")
    assert not journal.may_admit_new_task(clock(1000))
    assert not journal.may_call_model(clock(1000), purpose="task")
    assert journal.may_call_model(clock(1000), purpose="closeout")
    assert journal.may_call_model(clock(1299), purpose="closeout")
    assert not journal.may_call_model(clock(1300), purpose="closeout")
    with pytest.raises(CloseoutError, match="immutable"):
        AttemptCloseoutJournal(journal.directory, pin(cutoff_epoch=1001))


def test_pinned_outage_policy_is_task_scoped_and_does_not_extend_cutoff(tmp_path):
    directory = tmp_path / "attempt"
    directory.mkdir(mode=0o700)
    finish = AttemptCloseoutJournal(directory, pin(outage_policy="finish-current-task"))
    finish.append("run", "process_observed", 990,
                  {"invocation_id": "invocation-1", "state": "running"})
    finish.append("lost", "contact_lost", 995)
    assert not finish.may_admit_new_task(clock(999))
    assert finish.may_call_model(clock(999), purpose="task")
    assert not finish.may_admit_new_task(clock(1000))
    assert not finish.may_call_model(clock(1000), purpose="task")
    assert finish.may_call_model(clock(1000), purpose="closeout")
    assert finish.may_call_model(clock(1299), purpose="closeout")
    assert not finish.may_call_model(clock(1300), purpose="closeout")

    stop_dir = tmp_path / "stop"
    stop_dir.mkdir(mode=0o700)
    stop = AttemptCloseoutJournal(stop_dir, pin(outage_policy="checkpoint-stop"))
    stop.append("run", "process_observed", 990,
                {"invocation_id": "invocation-1", "state": "running"})
    stop.append("lost", "contact_lost", 995)
    assert not stop.may_call_model(clock(999), purpose="task")


def test_stop_request_is_not_physical_stop_and_unknown_keeps_exposure(journal):
    journal.append("run", "process_observed", 990,
                   {"invocation_id": "invocation-1", "state": "running"})
    state = journal.append("stop", "operator_stop_requested", 991)
    assert state["operator_stop"] and state["process"] == "running"
    assert state["exposure_held"]
    assert not journal.may_call_model(clock(992), purpose="closeout")
    state = journal.append("reconnect", "contact_restored", 992)
    assert state["operator_stop"]
    assert not journal.may_call_model(clock(992), purpose="closeout")
    state = journal.append("unknown", "process_observed", 993,
                           {"invocation_id": "invocation-1", "state": "unknown"})
    assert state["exposure_held"] and state["process"] == "unknown"
    state = journal.append("exit", "process_observed", 995,
                           {"invocation_id": "invocation-1", "state": "exited"})
    assert state["exposure_held"]
    journal.append("stop-seen", "operator_stop_observed", 996,
                   {"invocation_id": "invocation-1"})
    assert journal.snapshot()["stop_observed"]
    journal.append("release", "exposure_reconciled", 997,
                   {"invocation_id": "invocation-1"})
    assert not journal.snapshot()["exposure_held"]
    with pytest.raises(CloseoutError, match="Terminal process"):
        journal.append("stale-unknown", "process_observed", 998,
                       {"invocation_id": "invocation-1", "state": "unknown"})


def test_denied_policy_check_still_durably_parks_expired_time(journal):
    with pytest.raises(CloseoutError, match="ClockObservation"):
        journal.may_admit_new_task(1000)
    assert not journal.may_call_model(clock(1300), purpose="task")
    state = journal.snapshot()
    assert state["last_effective_epoch"] == 1300
    journal.append("late-running", "process_observed", 1301,
                   {"invocation_id": "invocation-1", "state": "running"})
    assert not journal.may_call_model(clock(999), purpose="task")


def test_wrong_invocation_conflicts_and_never_clears_uncertain_exposure(journal):
    journal.append("pinned-exit", "process_observed", 998,
                   {"invocation_id": "invocation-1", "state": "exited"})
    state = journal.append("foreign", "process_observed", 999,
                           {"invocation_id": "replacement", "state": "exited"})
    assert state["conflict"] and state["exposure_held"] and state["process"] == "exited"
    assert not journal.may_admit_new_task(clock(999))
    assert not journal.may_call_model(clock(999), purpose="closeout")
    with pytest.raises(CloseoutError, match="Exposure release"):
        journal.append("release", "exposure_reconciled", 1000,
                       {"invocation_id": "invocation-1"})
    assert journal.snapshot()["exposure_held"]


def test_clock_jump_parks_until_exact_server_reconciliation(journal):
    journal.append("running", "process_observed", 900,
                   {"invocation_id": "invocation-1", "state": "running"})
    journal.append("clock-1", "clock_sample", 900,
                   {"boot_id": "boot-1", "wall_epoch": 900,
                    "monotonic_ns": 900_000_000_000, "boottime_ns": 900_000_000_000})
    state = journal.append("clock-2", "clock_sample", 901,
                          {"boot_id": "boot-2", "wall_epoch": 901,
                           "monotonic_ns": 901_000_000_000, "boottime_ns": 901_000_000_000})
    assert state["clock_parked"]
    journal.append("reconnect", "contact_restored", 902)
    reopened = AttemptCloseoutJournal(journal.directory, pin())
    with pytest.raises(CloseoutError, match="ClockObservation"):
        reopened.may_call_model(903, purpose="task")
    assert not reopened.may_call_model(clock(903, "boot-2"), purpose="task")
    assert reopened.snapshot()["clock_parked"]
    with pytest.raises(CloseoutError, match="pinned authority"):
        journal.append("bad-reconcile", "server_reconcile", 904,
                       {"approval_id": "approval-1", "cutoff_epoch": 1001,
                        "claim_fence": 7, "source_head": pin().source_head, "server_now": 904})
    state = journal.append("reconcile", "server_reconcile", 904,
                           {"approval_id": "approval-1", "cutoff_epoch": 1000,
                            "claim_fence": 7, "source_head": pin().source_head, "server_now": 904})
    assert not state["clock_parked"]
    assert journal.may_call_model(clock(905, "boot-2"), purpose="task")
    assert not journal.may_admit_new_task(clock(1000, "boot-2"))


def test_rollback_and_suspend_park_and_late_events_do_not_regress_effective_time(journal):
    journal.append("a", "clock_sample", 1000,
                   {"boot_id": "boot-1", "wall_epoch": 1000,
                    "monotonic_ns": 1_000_000_000_000, "boottime_ns": 1_000_000_000_000})
    state = journal.append("b", "clock_sample", 900,
                           {"boot_id": "boot-1", "wall_epoch": 900,
                            "monotonic_ns": 1_001_000_000_000, "boottime_ns": 1_001_000_000_000})
    assert state["clock_parked"] and state["last_effective_epoch"] == 1000
    with pytest.raises(CloseoutError, match="Out-of-order"):
        journal.append("late", "contact_restored", 800)
    assert journal.snapshot()["last_effective_epoch"] == 1000

    suspend_dir = journal.directory.parent / "suspend"
    suspend_dir.mkdir(mode=0o700)
    suspend = AttemptCloseoutJournal(suspend_dir, pin())
    suspend.append("before-suspend", "clock_sample", 1000,
                   {"boot_id": "boot-1", "wall_epoch": 1000,
                    "monotonic_ns": 1_000_000_000_000, "boottime_ns": 1_000_000_000_000})
    state = suspend.append("after-suspend", "clock_sample", 1001,
                           {"boot_id": "boot-1", "wall_epoch": 1001,
                            "monotonic_ns": 1_001_000_000_000, "boottime_ns": 1_060_000_000_000})
    assert state["clock_parked"]


def test_replay_identity_sequence_lock_and_private_durable_record(journal, monkeypatch):
    with pytest.raises(CloseoutError, match="Contact loss"):
        journal.append("bad-contact", "contact_lost", 899, {"reason": "ignored"})
    payload = {"invocation_id": "invocation-1", "state": "running"}
    first = journal.append("same", "process_observed", 900, payload, expected_sequence=0)
    replay = journal.append("same", "process_observed", 900, payload, expected_sequence=0)
    assert first == replay
    with pytest.raises(CloseoutError, match="different payload"):
        journal.append("same", "process_observed", 901,
                       {"invocation_id": "invocation-1", "state": "unknown"})
    with pytest.raises(CloseoutError, match="sequence changed"):
        journal.append("next", "contact_lost", 901, expected_sequence=0)
    file = journal.directory / "journal.json"
    assert stat.S_IMODE(file.stat().st_mode) == 0o600
    assert stat.S_IMODE((journal.directory / ".closeout.lock").stat().st_mode) == 0o600
    assert journal.snapshot() == AttemptCloseoutJournal(journal.directory, pin()).snapshot()
    assert json.loads(file.read_text())["events"][0]["prev_hash"] == "0" * 64


def test_checkpoint_is_task_owned_bounded_reference_not_a_path(journal):
    state = journal.append("checkpoint", "checkpoint", 900,
                           {"kind": "checkpoint", "artifact_ref": "artifact-17",
                            "summary": "paused after compiler step"})
    resume = journal.append("resume", "checkpoint", 901,
                            {"kind": "resume", "artifact_ref": "artifact-18",
                             "summary": "resume from the recorded compiler step"})
    assert resume["summaries"] == [
        {"task_id": "task-1", "kind": "checkpoint", "artifact_ref": "artifact-17",
         "summary": "paused after compiler step"},
        {"task_id": "task-1", "kind": "resume", "artifact_ref": "artifact-18",
         "summary": "resume from the recorded compiler step"},
    ]
    with pytest.raises(CloseoutError, match="identifier"):
        journal.append("path", "checkpoint", 901,
                       {"kind": "checkpoint", "artifact_ref": "../../secret", "summary": "not a path"})


def test_corrupt_hash_chain_is_refused(journal):
    journal.append("lost", "contact_lost", 900)
    file = journal.directory / "journal.json"
    record = json.loads(file.read_text())
    record["events"][0]["at"] = 901
    file.write_text(json.dumps(record, sort_keys=True, separators=(",", ":")))
    with pytest.raises(CloseoutError, match="hash"):
        journal.snapshot()
