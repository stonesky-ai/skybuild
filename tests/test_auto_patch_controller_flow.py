"""The bounded controller owns both claims and both launch outcomes."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from skybuild import auto_patch_controller as controller
from skybuild import manual_cord, manual_dispatch


def _items(tmp_path):
    result = []
    for index in (1, 2):
        task_id = f"SKYBUILD-CPU-{index}"
        worker = f"worker_{index}"
        envelope = {"schema": "manual-work-v2", "task_id": task_id,
                    "assignment_id": f"ASSIGN-{index}", "worker": worker,
                    "base_sha": "a" * 40, "brief_sha256": "b" * 64,
                    "task_status": "ready", "task_revision": 2}
        result.append({"task_id": task_id, "worker": worker, "assignment_id": f"ASSIGN-{index}",
                       "branch": f"task/cpu-{index}", "brief_path": f"docs/design/assignments/{index}.json",
                       "patch": str(tmp_path / f"patch-{index}"), "patch_sha256": "c" * 64,
                       "token_file": str(tmp_path / worker), "git_token_file": str(tmp_path / f"git-{index}"),
                       "priority": index, "revision": 2, "base_sha": "a" * 40,
                       "brief_sha256": "b" * 64, "envelope": envelope})
    return result


def _setup(tmp_path, monkeypatch, *, fail_at=None):
    repo = tmp_path / "repo"
    repo.mkdir()
    selected = _items(tmp_path)
    expiry = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    permit = {"approved_until": expiry, "memory_high_bytes": 1024**3,
              "memory_max_bytes": 2 * 1024**3, "runtime_seconds": 600}
    events = []
    monkeypatch.setattr(controller, "_private_endpoint", lambda *_args: "private")
    monkeypatch.setattr(controller, "_read_manifest", lambda _path: selected)
    monkeypatch.setattr(controller, "ca_file_sha256", lambda _path: "d" * 64)
    monkeypatch.setattr(controller, "_token_from_file", lambda path: Path(path).name)
    monkeypatch.setattr(controller, "select", lambda *_args, **_kwargs: selected)
    monkeypatch.setattr(controller, "load_permit", lambda *_args, **_kwargs: permit)
    monkeypatch.setattr(controller, "check_source", lambda *_args: events.append("source"))
    monkeypatch.setattr(controller, "check_weekly_usage", lambda *_args: events.append("usage"))
    monkeypatch.setattr(controller, "resource_admission", lambda *_args, **_kwargs: events.append("host"))
    monkeypatch.setattr(controller, "probe_private_api", lambda *_args, **_kwargs: events.append("worker_token"))
    monkeypatch.setattr(controller, "_approved_task", lambda _task, envelope, _digest:
                        events.append("ready:" + envelope["task_id"]))

    class FakeClient:
        def __init__(self, _url, token, **_kwargs):
            self.token = token
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            pass
        def whoami(self):
            return {"is_admin": self.token == "owner"}
        def cpu_control_status(self, _project):
            events.append("owner_controls")
            return {"pool": {"enabled": True, "local_enabled": True, "capacity": 2},
                    "held_units": 0}
        def get_task(self, _project, task_id):
            return {"task_id": task_id, "status": "ready", "revision": 2}

    monkeypatch.setattr(controller, "Client", FakeClient)

    def dispatch(_repo, _brief, *, expected_envelope, worker, **_kwargs):
        events.append("dispatch:" + worker)
        assert expected_envelope == selected[int(worker[-1]) - 1]["envelope"]
        return {"message_id": "message-" + worker}

    monkeypatch.setattr(controller, "dispatch", dispatch)

    def receive(_client, _project, _repo, *, worker, expected_envelope, destination, **_kwargs):
        events.append("claim:" + worker)
        assert expected_envelope == selected[int(worker[-1]) - 1]["envelope"]
        if fail_at == "claim-2" and worker == "worker_2":
            raise controller.ManualCordError("claim failed")
        destination.write_text(json.dumps(expected_envelope))
        return {"place": "working", "attempt_id": "attempt-" + worker,
                "claim_fence": 1, "assignment_id": expected_envelope["assignment_id"],
                "message_id": "message-" + worker}

    monkeypatch.setattr(controller, "receive_assignment", receive)

    class FakeManager:
        def __init__(self, _state_dir):
            self.started = set()
        def start(self, spec):
            events.append("launch:" + spec.task_id)
            assert spec.memory_max_bytes == 2 * 1024**3 and spec.runtime_seconds <= 600
            assert spec.argv[spec.argv.index("--permit-sha256") + 1] == "f" * 64
            assert spec.argv[spec.argv.index("--permit") + 1] == str(tmp_path / "permit")
            if fail_at == "launch-2" and spec.task_id.endswith("-2"):
                raise controller.JobUnitError("unknown second launch")
            self.started.add(spec.unit())
            argv = list(spec.argv)
            directory = Path(argv[argv.index("--state-dir") + 1])
            assignment = selected[int(directory.name[-1]) - 1]["assignment_id"]
            (directory / "submitted.json").write_text(json.dumps({"assignment_id": assignment,
                "sent": True, "message_id": "result-1", "head_sha": "e" * 40}))
            return spec.unit()
        def observe(self, unit):
            if unit not in self.started:
                raise controller.JobUnitError("unconfirmed unit")
            return SimpleNamespace(phase="completed", result="success", exit_status=0,
                                   memory_peak_bytes=1024**2)

    monkeypatch.setattr(controller, "JobUnitManager", FakeManager)
    if fail_at == "launch-2":
        def bounded_observe(manager, owned, _deadline):
            for record in owned:
                try:
                    state = manager.observe(record["unit"])
                    record.update(phase=state.phase, result=state.result,
                                  exit_status=state.exit_status)
                except controller.JobUnitError:
                    record.update(phase="unknown", result=None, exit_status=None)
            return owned
        monkeypatch.setattr(controller, "_observe_owned", bounded_observe)
    return repo, selected, events


@pytest.mark.parametrize("failure,expected_claims,expected_launches", [
    (None, 2, 2), ("claim-2", 2, 1), ("launch-2", 2, 2),
])
def test_controller_dispatches_claims_and_bounds_two_distinct_attempts(
        tmp_path, monkeypatch, failure, expected_claims, expected_launches):
    repo, selected, events = _setup(tmp_path, monkeypatch, fail_at=failure)
    output = controller.run(repo=repo, manifest=tmp_path / "manifest", project="skybuild",
        dispatcher="pilot_dispatcher", url="https://private.ts.net", dispatcher_token=tmp_path / "dispatcher",
        owner_token=tmp_path / "owner", ca_file=tmp_path / "ca", base_ref="refs/heads/dev-006",
        state_dir=tmp_path / "run", permit_path=tmp_path / "permit", permit_sha256="f" * 64,
        weekly_usage=tmp_path / "weekly", hostwatch=tmp_path / "host")
    assert events.count("dispatch:worker_1") == events.count("dispatch:worker_2") == 1
    assert sum(value.startswith("claim:") for value in events) == expected_claims
    assert sum(value.startswith("launch:") for value in events) == expected_launches
    assert output["submitted"] is (failure is None)
    assert len(output["workers"]) == (2 if failure == "launch-2" or failure is None else 1)
    assert json.loads((tmp_path / "run" / "outcome.json").read_text()) == output
    assert (tmp_path / "run" / "selection.json").is_file()
    assert all(item["envelope"]["base_sha"] == "a" * 40 for item in selected)
    if failure == "launch-2":
        assert output["workers"][1]["phase"] == "unknown"
        assert (tmp_path / "run" / "worker-worker_2.launch-intent.json").is_file()


def test_dispatch_rejects_rebuilt_development_base_before_cord_send(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    brief = "docs/design/assignments/one.json"
    old = {"schema": "manual-work-v2", "task_id": "SKYBUILD-CPU-1", "base_sha": "a" * 40,
           "brief_sha256": "b" * 64, "task_status": "ready", "task_revision": 2}
    refreshed = {"schema": "manual-work-v1", "task_id": "SKYBUILD-CPU-1",
                 "base_sha": "c" * 40, "brief_sha256": "b" * 64}
    monkeypatch.setattr(manual_dispatch, "_private_endpoint", lambda *_args: "private")
    monkeypatch.setattr(manual_dispatch, "build_envelope", lambda *_args, **_kwargs: refreshed)
    def forbidden_client(*_args, **_kwargs):
        pytest.fail("Cord client must not open after a selected base drift")
    with pytest.raises(manual_dispatch.DispatchError, match="approved exact envelope"):
        manual_dispatch.dispatch(repo, brief, worker="worker_1", dispatcher="pilot_dispatcher",
            project="skybuild", principal="pilot_dispatcher", url="https://private.ts.net",
            token_file=tmp_path / "token", state_dir=tmp_path / "dispatch",
            base_ref="refs/heads/dev-006", client_factory=forbidden_client,
            expected_envelope=old)


def test_receive_refuses_changed_cord_envelope_before_claim(tmp_path):
    class FakeClient:
        def inbox(self, *_args, **_kwargs):
            return [{"message_id": "message-1", "sender": "pilot_dispatcher",
                     "recipient": "worker_1", "category": "manual-work",
                     "body": json.dumps({"assignment_id": "different"})}]
        def get_task(self, *_args):
            pytest.fail("Task claim path must not read after envelope drift")
    destination = tmp_path / "assignment.json"
    with pytest.raises(manual_cord.ManualCordError, match="approved exact envelope"):
        manual_cord.receive_assignment(FakeClient(), "skybuild", tmp_path, worker="worker_1",
            dispatcher="pilot_dispatcher", message_id="message-1", destination=destination,
            expected_envelope={"assignment_id": "approved"})
    assert not destination.exists()
