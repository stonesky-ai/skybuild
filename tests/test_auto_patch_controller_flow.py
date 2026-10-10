"""The bounded controller owns both claims and both launch outcomes."""

import json
import hashlib
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
        patch_path = tmp_path / f"patch-{index}"
        patch_path.write_bytes(b"approved patch " + str(index).encode())
        patch_path.chmod(0o600)
        envelope = {"schema": "manual-work-v2", "task_id": task_id,
                    "assignment_id": f"ASSIGN-{index}", "worker": worker,
                    "base_sha": "a" * 40, "brief_sha256": "b" * 64,
                    "task_status": "ready", "task_revision": 2}
        result.append({"task_id": task_id, "worker": worker, "assignment_id": f"ASSIGN-{index}",
                       "branch": f"task/cpu-{index}", "brief_path": f"docs/design/assignments/{index}.json",
                       "patch": str(patch_path),
                       "patch_sha256": hashlib.sha256(patch_path.read_bytes()).hexdigest(),
                       "token_file": str(tmp_path / worker),
                       "priority": index, "revision": 2, "base_sha": "a" * 40,
                       "brief_sha256": "b" * 64, "envelope": envelope})
    return result


def _setup(tmp_path, monkeypatch, *, fail_at=None):
    repo = tmp_path / "repo"
    repo.mkdir()
    selected = _items(tmp_path)
    (tmp_path / "manifest").write_text(json.dumps(selected))
    expiry = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    permit = {"approved_until": expiry, "memory_high_bytes": 1024**3,
              "memory_max_bytes": 2 * 1024**3, "runtime_seconds": 600,
              "worker_image_id": "sha256:" + "a" * 64}
    events = []
    monkeypatch.setattr(controller, "_private_endpoint", lambda *_args: "private")
    monkeypatch.setattr(controller, "_read_manifest", lambda _path: selected)
    monkeypatch.setattr(controller, "ca_file_sha256", lambda _path: "d" * 64)
    monkeypatch.setattr(controller, "_token_from_file", lambda path: Path(path).name)
    monkeypatch.setattr(controller, "select", lambda *_args, **_kwargs: selected)
    monkeypatch.setattr(controller, "load_permit", lambda *_args, **_kwargs: permit)
    def source_mount(_repo, destination, _base):
        destination.mkdir(mode=0o700)
        return destination
    monkeypatch.setattr(controller, "_prepare_worker_source", source_mount)
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
            return {"pool": {"enabled": True, "local_enabled": True, "capacity": 2,
                             "generation": 3, "local_generation": 4},
                    "held_units": 0}
        def get_task(self, _project, task_id):
            return {"task_id": task_id, "status": "in-progress", "revision": 3,
                    "metadata": {"_skybuild_workflow": {"readiness": {"input_generation": 5}}}}
        def reserve_cpu(self, _project, request):
            events.append("reserve:" + request["task_id"])
            return {"action_id": request["action_id"], "attempt_id": request["attempt_id"],
                    "state": "reserved"}

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
        destination.chmod(0o600)
        return {"place": "working", "attempt_id": "attempt-" + worker,
                "claim_fence": 1, "assignment_id": expected_envelope["assignment_id"],
                "message_id": "message-" + worker}

    monkeypatch.setattr(controller, "receive_assignment", receive)

    prepared_by_id = {}
    monkeypatch.setattr(controller, "prepare_worker", lambda plan, *, action_id, operation_id:
        prepared_by_id.setdefault(operation_id, SimpleNamespace(plan=plan, action_id=action_id,
            operation_id=operation_id, unit_name="skybuild-job-" + plan.worker_id[-1] * 24 + ".service",
            task_id=plan.worker_id, launch_nonce="nonce-" + plan.worker_id,
            assignment_id="ASSIGN-" + plan.worker_id[-1],
            attempt_id="attempt-" + plan.worker_id, claim_fence=1,
            container_name="container-" + plan.worker_id,
            container_run_id="run-" + plan.worker_id,
            spec=SimpleNamespace(log_path=plan.external_state_dir / "worker.log"),
            approved_until=datetime.fromisoformat(expiry), source_head="a" * 40,
            controller_head="a" * 40,
            **{name: "d" * 64 for name in ("source_digest", "interpreter_digest",
                "controller_source_digest", "controller_profile_digest", "ca_digest",
                "owner_token_digest", "worker_token_digest", "assignment_digest", "argv_digest")})))
    launched = set()
    def launch(prepared):
        events.append("launch:" + prepared.task_id)
        if fail_at == "launch-2" and prepared.plan.worker_id == "worker_2":
            raise controller.JobUnitError("unknown second launch")
        launched.add(prepared.unit_name)
        return {"started": True, "unit_name": prepared.unit_name}
    monkeypatch.setattr(controller, "launch_worker", launch)
    monkeypatch.setattr(controller, "_docker_container_state", lambda _prepared:
                        {"status": "exited", "exit_code": 2})

    def reconcile(prepared):
        events.append("reconcile:" + prepared.task_id)
        if prepared.unit_name not in launched:
            return {"submitted": False, "settled": False, "reason": "unit unknown"}
        receipt = {"assignment_id": "ASSIGN-" + prepared.plan.worker_id[-1],
                   "sent": True, "message_id": "relay-" + prepared.plan.worker_id,
                   "head_sha": "e" * 40}
        path = prepared.plan.assignment_dir / "submitted.json"
        path.write_text(json.dumps(receipt))
        path.chmod(0o600)
        return {"submitted": True, "settled": True}
    monkeypatch.setattr(controller, "reconcile_worker", reconcile)

    class FakeManager:
        def __init__(self, _state_dir):
            pass
        def observe(self, unit):
            if unit not in launched:
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
        weekly_usage=tmp_path / "weekly", hostwatch=tmp_path / "host",
        controller_profile=tmp_path / "controller-profile")
    assert events.count("dispatch:worker_1") == events.count("dispatch:worker_2") == 1
    assert sum(value.startswith("claim:") for value in events) == expected_claims
    assert sum(value.startswith("launch:") for value in events) == expected_launches
    assert output["submitted"] is (failure is None)
    assert len(output["workers"]) == (2 if failure == "launch-2" or failure is None else 1)
    assert json.loads((tmp_path / "run" / "outcome.json").read_text()) == output
    assert (tmp_path / "run" / "selection.json").is_file()
    assert all(item["envelope"]["base_sha"] == "a" * 40 for item in selected)
    if failure == "launch-2":
        assert output["workers"][1].get("reconciliation_error") == "JobUnitError"
        assert (tmp_path / "run" / "worker-worker_2" / "launch-intent.json").is_file()


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
