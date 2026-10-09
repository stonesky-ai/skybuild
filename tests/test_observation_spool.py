"""Crash, retry and identity boundaries for local observation delivery."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
import stat
from uuid import uuid4

import pytest

from skybuild.contracts import DomainError
from skybuild.observation_spool import IDENTITY, ObservationSpool, SpoolError
from test_store import store, actors  # noqa: F401: disposable database fixtures


def packet(**changes):
    return dict(event_id=uuid4().hex, task_id="task-1", attempt_id="attempt-1",
                claim_fence=1, component_id="worker", source_id="observer", boot_id="boot-1",
                pid=12, process_start="monotonic-100", source_sequence=1,
                observed_at="2026-10-09T09:00:00+01:00", state="running",
                evidence_refs=["artifact-1"], **changes)


def accepted(project, body):
    return dict(project_id=project, event_id=body["event_id"],
                identity={key: body[key] for key in IDENTITY},
                source_sequence=body["source_sequence"], state=body["state"],
                observed_at=datetime.fromisoformat(body["observed_at"]).astimezone(timezone.utc).isoformat(),
                evidence_refs=body["evidence_refs"])


@pytest.fixture
def directory(tmp_path):
    path = tmp_path / "spool"
    path.mkdir(mode=0o700)
    return path


def test_lost_reply_restarts_with_identical_packet_and_sequence(directory):
    spool = ObservationSpool(directory)
    body = packet()
    spool.enqueue("skybuild", body)
    seen = []
    server = {}

    def lost_reply(project, body):
        seen.append(body)
        server[body["event_id"]] = accepted(project, body)
        raise TimeoutError("Lost accepted reply; token must not reach disk")

    assert spool.replay(lost_reply)[0].status == "unknown"
    restarted = ObservationSpool(directory)
    pending = restarted.pending()
    assert pending[0]["packet"] == body
    assert pending[0]["last_outcome"] == {"status": "unknown", "status_code": None}
    assert b"token" not in next(directory.glob("*.json")).read_bytes()

    def retry(project, retry_body):
        seen.append(retry_body)
        assert retry_body == body
        return server[retry_body["event_id"]]

    assert restarted.replay(retry)[0].status == "acknowledged"
    assert seen == [body, body]
    assert restarted.pending() == []


def test_packets_are_private_and_fsynced_before_transport(directory, monkeypatch):
    real_fsync = os.fsync
    synced = []

    def fsync(fd):
        synced.append(stat.S_IFMT(os.fstat(fd).st_mode))
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    spool = ObservationSpool(directory)
    body = packet()
    spool.enqueue("skybuild", body)
    body["evidence_refs"].append("caller-mutation")

    def transport(project, stored):
        assert stat.S_IFREG in synced and stat.S_IFDIR in synced
        assert stored["evidence_refs"] == ["artifact-1"]
        for file in directory.iterdir():
            assert stat.S_IMODE(file.stat().st_mode) == 0o600
        return accepted(project, stored)

    assert spool.replay(transport)[0].status == "acknowledged"


@pytest.mark.parametrize("failure", ["write", "file-fsync", "rename", "directory-fsync"])
def test_interrupted_publication_never_transmits_partial_packet(directory, monkeypatch, failure):
    spool = ObservationSpool(directory)
    body = packet()
    with monkeypatch.context() as patch:
        if failure == "write":
            write = os.write

            def partial(fd, data):
                write(fd, data[:20])
                raise OSError("Interrupted write")

            patch.setattr(os, "write", partial)
        elif failure == "rename":
            def rename(*args, **kwargs):
                raise OSError("Interrupted rename")
            patch.setattr(os, "rename", rename)
        else:
            fsync = os.fsync

            def interrupted_sync(fd):
                is_dir = stat.S_ISDIR(os.fstat(fd).st_mode)
                if is_dir == (failure == "directory-fsync"):
                    raise OSError("Interrupted fsync")
                fsync(fd)

            patch.setattr(os, "fsync", interrupted_sync)
        with pytest.raises(OSError, match="Interrupted"):
            spool.enqueue("skybuild", body)
    restarted = ObservationSpool(directory)
    records = restarted.pending()
    assert all(record["packet"] == body for record in records)
    # Retry either reuses the published packet or safely publishes it afresh.
    restarted.enqueue("skybuild", body)
    assert restarted.replay(accepted)[0].status == "acknowledged"
    assert restarted.pending() == []


def test_crash_temporary_file_is_never_replayed(directory):
    spool = ObservationSpool(directory)
    temporary = directory / (".pending-" + uuid4().hex)
    temporary.write_bytes(b'{"partial":')
    temporary.chmod(0o600)
    assert spool.replay(lambda *args: pytest.fail("Partial write reached transport")) == []
    assert not temporary.exists()


@pytest.mark.parametrize("response", [None, {}, {"ok": True}, {"code": "idempotency_conflict"}])
def test_unknown_and_conflict_responses_survive_restart(directory, response):
    spool = ObservationSpool(directory)
    body = packet()
    spool.enqueue("skybuild", body)
    outcome = spool.replay(lambda *args: response)[0]
    expected = "conflict" if response == {"code": "idempotency_conflict"} else "unknown"
    assert outcome.status == expected
    assert outcome.response == response
    pending = ObservationSpool(directory).pending()[0]
    assert pending["packet"] == body
    assert pending["last_outcome"]["status"] == expected
    assert spool.replay(accepted)[0].status == "acknowledged"


def test_api_unavailable_and_domain_conflict_preserve_original_evidence(directory):
    spool = ObservationSpool(directory)
    body = packet()
    spool.enqueue("skybuild", body)
    for error, expected in ((ConnectionError("API unavailable"), "unknown"),
                            (DomainError("idempotency_conflict", "different evidence", 409), "conflict")):
        def fail(*args):
            raise error
        outcome = spool.replay(fail)[0]
        assert (outcome.status, outcome.response) == (expected, error)
        assert ObservationSpool(directory).pending()[0]["packet"] == body
    assert ObservationSpool(directory).pending()[0]["last_outcome"] == {"status": "conflict", "status_code": 409}


@pytest.mark.parametrize("change", [{"boot_id": "boot-2"}, {"pid": 13}, {"process_start": "monotonic-200"}])
def test_changed_process_identity_keeps_distinct_packets_and_rejects_wrong_ack(directory, change):
    spool = ObservationSpool(directory)
    first = packet()
    second = dict(first, event_id=uuid4().hex, **change)
    spool.enqueue("skybuild", first)
    spool.enqueue("skybuild", second)

    def wrong_identity(project, body):
        response = accepted(project, body)
        response["identity"] = dict(response["identity"], boot_id="wrong-boot")
        return response

    assert all(outcome.status == "unknown" for outcome in spool.replay(wrong_identity))
    assert {json.dumps(item["packet"], sort_keys=True) for item in spool.pending()} == {
        json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True)}
    assert all(outcome.status == "acknowledged" for outcome in spool.replay(accepted))


def test_full_spool_refuses_without_overwriting_and_replay_is_bounded(directory):
    spool = ObservationSpool(directory, max_records=2)
    first, second, third = packet(), packet(), packet()
    spool.enqueue("skybuild", first)
    spool.enqueue("skybuild", second)
    before = {p.name: p.read_bytes() for p in directory.glob("*.json")}
    with pytest.raises(SpoolError, match="full"):
        spool.enqueue("skybuild", third)
    assert before == {p.name: p.read_bytes() for p in directory.glob("*.json")}
    assert len(spool.replay(accepted, limit=1)) == 1
    assert len(spool.pending()) == 1
    spool.enqueue("skybuild", third)
    with pytest.raises(SpoolError, match="Replay limit"):
        spool.replay(accepted, limit=3)


def test_byte_capacity_duplicate_and_changed_event_refusal(directory):
    spool = ObservationSpool(directory, max_bytes=800)
    first = packet()
    spool.enqueue("skybuild", first)
    spool.enqueue("skybuild", first)
    with pytest.raises(SpoolError, match="full"):
        spool.enqueue("skybuild", packet())
    with pytest.raises(SpoolError, match="different evidence"):
        spool.enqueue("skybuild", dict(first, state="exited"))
    assert spool.pending()[0]["packet"] == first


@pytest.mark.parametrize("changes", [{"pid": True}, {"boot_id": None}, {"source_sequence": 0},
                                     {"state": "done"}, {"observed_at": "2026-10-09"},
                                     {"evidence_refs": ["/secret"]}, {"extra": "not accepted"}])
def test_invalid_packets_never_publish(directory, changes):
    spool = ObservationSpool(directory)
    with pytest.raises(SpoolError):
        spool.enqueue("skybuild", dict(packet(), **changes))
    assert spool.pending() == []


def test_symlinks_permissions_and_corruption_refuse_without_transport(directory, tmp_path):
    directory.chmod(0o755)
    with pytest.raises(SpoolError, match="private"):
        ObservationSpool(directory)
    directory.chmod(0o700)
    link = tmp_path / "link"
    link.symlink_to(directory, target_is_directory=True)
    with pytest.raises(OSError):
        ObservationSpool(link)
    spool = ObservationSpool(directory)
    spool.enqueue("skybuild", packet())
    file = next(directory.glob("*.json"))
    original = file.read_bytes()
    file.write_bytes(b'{"broken":')
    with pytest.raises(SpoolError, match="invalid"):
        spool.replay(lambda *args: pytest.fail("Corrupt packet reached transport"))
    file.write_bytes(original)
    file.chmod(0o644)
    with pytest.raises(SpoolError, match="private"):
        spool.pending()


def test_concurrent_enqueue_keeps_one_immutable_record(directory):
    spool = ObservationSpool(directory)
    body = packet()
    with ThreadPoolExecutor(max_workers=4) as workers:
        assert list(workers.map(lambda _: spool.enqueue("skybuild", body), range(8))) == [body["event_id"]] * 8
    assert len(spool.pending()) == 1


@pytest.mark.parametrize("field,value", [("project_id", "other"), ("event_id", "other"),
                                         ("source_sequence", 2), ("state", "exited"),
                                         ("observed_at", "2020-01-01T00:00:00+00:00"),
                                         ("evidence_refs", ["other"])])
def test_ack_must_match_all_evidence(directory, field, value):
    spool = ObservationSpool(directory)
    spool.enqueue("skybuild", packet())

    def wrong(project, body):
        return dict(accepted(project, body), **{field: value})

    assert spool.replay(wrong)[0].status == "unknown"
    assert len(spool.pending()) == 1


def test_packet_size_is_bounded_and_short_writes_are_completed(directory, monkeypatch):
    with pytest.raises(SpoolError, match="packet bound"):
        ObservationSpool(directory, max_packet_bytes=20).enqueue("skybuild", packet())
    write = os.write
    monkeypatch.setattr(os, "write", lambda fd, data: write(fd, data[:7]))
    spool = ObservationSpool(directory)
    body = packet()
    spool.enqueue("skybuild", body)
    assert spool.pending()[0]["packet"] == body
    assert spool.replay(accepted)[0].status == "acknowledged"


def test_record_symlink_never_reads_or_modifies_target(directory, tmp_path):
    spool = ObservationSpool(directory)
    spool.enqueue("skybuild", packet())
    file = next(directory.glob("*.json"))
    target = tmp_path / "private"
    target.write_text("unchanged")
    file.unlink()
    file.symlink_to(target)
    with pytest.raises(OSError):
        spool.replay(lambda *args: pytest.fail("Symlink reached transport"))
    assert target.read_text() == "unchanged"


def test_real_observation_receipt_replay_preserves_authority(directory, store, actors):
    from test_observations import reserved, packet as reserved_packet
    project, people = actors
    reservation = reserved(store, people, project)
    body = reserved_packet(reservation)
    spool = ObservationSpool(directory)
    spool.enqueue(project, body)

    def lose_reply(project_id, pending):
        store.record_observation(people["worker"], project_id, pending)
        raise TimeoutError("Accepted response lost")

    assert spool.replay(lose_reply)[0].status == "unknown"
    restarted = ObservationSpool(directory)
    outcome = restarted.replay(lambda project_id, pending: store.record_observation(people["worker"], project_id, pending))[0]
    assert outcome.status == "acknowledged"
    assert restarted.pending() == []
    with store._connection() as connection:
        assert connection.execute("SELECT count(*) AS n FROM observation_events WHERE project_id = %s AND event_id = %s",
                                  (project, body["event_id"])).fetchone()["n"] == 1
        assert connection.execute("SELECT state FROM cpu_reservations WHERE project_id = %s AND attempt_id = %s",
                                  (project, body["attempt_id"])).fetchone()["state"] == "reserved"
        assert connection.execute("SELECT held FROM task_claims WHERE project_id = %s AND task_id = %s",
                                  (project, body["task_id"])).fetchone()["held"] is True


def test_ack_timestamp_normalization_and_integer_types(directory):
    spool = ObservationSpool(directory)
    body = packet()
    spool.enqueue("skybuild", body)

    def wrong_type(project, pending):
        response = accepted(project, pending)
        response["source_sequence"] = True
        return response

    assert spool.replay(wrong_type)[0].status == "unknown"

    def alternate_timezone(project, pending):
        return dict(accepted(project, pending), observed_at=pending["observed_at"])

    assert spool.replay(alternate_timezone)[0].status == "acknowledged"
