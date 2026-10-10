"""PostgreSQL transaction checks for trusted CPU worker dispatch."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import sys
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from skybuild.api import create_app
from skybuild.contracts import DomainError
from skybuild.cpu_worker_dispatch import CPUWorkerDispatch
from skybuild.store import Store
from test_claims import ready
from test_store import seed_api_authority


@pytest.fixture
def cpu_dispatch(restricted_database, monkeypatch):
    # The shared usage module lands in migration 016's combined source bundle.
    # These migration-014 unit cases model a clear lineage while testing the
    # fail-closed missing-module case separately below.
    monkeypatch.setitem(sys.modules, "skybuild.task_usage",
                       SimpleNamespace(unresolved_usage_exists=lambda _connection, _project, _task: False))
    admin_dsn, runtime_dsn, database, _ = restricted_database
    admin, runtime = Store(admin_dsn, database), Store(runtime_dsn, database)
    project = "cpu-worker-" + uuid4().hex
    seed_api_authority(admin, project)
    people, tokens = {}, {}
    for name, grants in (("owner", {}), ("worker", {project: {"tasks:read", "tasks:write", "tasks:claim"}}),
                         ("outsider", {})):
        principal, token = name + "-" + uuid4().hex, uuid4().hex
        admin.provision_principal(principal, token, is_admin=name == "owner", grants=grants)
        people[name], tokens[name] = admin.authenticate(token), token
    with TestClient(create_app(runtime)) as client:
        yield admin, runtime, client, project, people, tokens


def _headers(token):
    return {"Authorization": "Bearer " + token}


def _make_reservation(runtime, people, project, task_id="cpu-dispatch-task", *, lease_seconds=60):
    task = ready(runtime, people, project, task_id)
    initialized = runtime.initialize_workflow(
        people["owner"], project, task_id, task["revision"], "initialize-" + task_id)["task"]
    claim = runtime.claim_task(people["worker"], project, task_id, initialized["revision"],
                               "claim-" + task_id, lease_seconds=lease_seconds)
    working = runtime.get_task(people["owner"], project, task_id)
    token = Store.workflow_token(working)
    with runtime._connection() as connection:
        readiness = connection.execute(
            "SELECT input_generation FROM task_readiness WHERE project_id = %s AND task_id = %s",
            (project, task_id)).fetchone()["input_generation"]
    status = runtime.cpu_control_status(people["owner"], project)
    if status["pool"] is None:
        pool = runtime.configure_cpu_pool(people["owner"], project, 1, True, 0, "pool-" + task_id,
                                          reason="Enable test CPU pool")
        runtime.set_cpu_local_control(people["owner"], project, True, pool["local_generation"],
                                      "local-" + task_id, reason="Enable test local control")
        status = runtime.cpu_control_status(people["owner"], project)
    request = dict(task_id=task_id, action_id=uuid4().hex, attempt_id=token.attempt_id, units=1,
                   expected_revision=working["revision"], readiness_generation=readiness,
                   claim_fence=claim["fence"], generation=status["pool"]["generation"],
                   local_generation=status["pool"]["local_generation"])
    reservation = runtime.reserve_cpu(people["worker"], project, **request)
    return request, reservation, working


def _pins(action_id, operation_id, *, unit_suffix=None):
    return dict(
        action_id=action_id,
        operation_id=operation_id,
        profile_id="bounded-trusted-cpu-patch-v1",
        host_id="trusted-host",
        worker_id="worker-" + uuid4().hex,
        unit_name="skybuild-job-" + (unit_suffix or uuid4().hex[:24]) + ".service",
        launch_nonce=uuid4().hex,
        source_digest="1" * 64,
        controller_head="2" * 40,
        controller_source_digest="3" * 64,
        controller_profile_digest="4" * 64,
        interpreter_digest="5" * 64,
        permit_digest="6" * 64,
        assignment_digest="7" * 64,
        patch_digest="8" * 64,
        argv_digest="9" * 64,
        approved_until=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    )


def _prepare(client, project, token, pins):
    return client.post(f"/api/v1/projects/{project}/cpu-worker-dispatches/prepare",
                       headers=_headers(token), json=pins)


def _unit_identity(pins, invocation="a" * 32):
    return {"host_id": pins["host_id"], "unit_name": pins["unit_name"],
            "launch_nonce": pins["launch_nonce"], "invocation_id": invocation}


def _settleable_observation(pins, observation_id, invocation="a" * 32):
    return {**_unit_identity(pins, invocation), "observation_id": str(observation_id),
            "phase": "completed", "result": "success", "exit_status": 0,
            "worker_result_digest": "f" * 64}


def _persisted(admin, project, action_id, operation_id):
    with admin._connection() as connection:
        dispatch = connection.execute(
            "SELECT * FROM cpu_worker_dispatches WHERE project_id = %s AND operation_id = %s",
            (project, operation_id)).fetchone()
        effect = connection.execute("SELECT * FROM task_effects WHERE operation_id = %s", (operation_id,)).fetchone()
        reservation = connection.execute("SELECT * FROM cpu_reservations WHERE action_id = %s", (action_id,)).fetchone()
        observations = connection.execute(
            "SELECT * FROM cpu_worker_observations WHERE operation_id = %s ORDER BY sequence", (operation_id,)).fetchall()
    return dispatch, effect, reservation, observations


def test_migration_014_digest_and_append_only_dispatch_guards(cpu_dispatch):
    admin, runtime, client, project, people, tokens = cpu_dispatch
    with admin._connection() as connection:
        applied = connection.execute("SELECT digest FROM schema_migrations WHERE version = 14").fetchone()
    assert applied["digest"] == "498059578f825d951f0c2d2cd4e3ed6ef56ecfc03dd34fb9656dffb91e5f09fe"

    request, _, _ = _make_reservation(runtime, people, project)
    pins = _pins(request["action_id"], "operation-" + uuid4().hex)
    assert _prepare(client, project, tokens["owner"], pins).status_code == 200
    with pytest.raises(psycopg.Error):
        with admin._connection() as connection:
            connection.execute("UPDATE cpu_worker_dispatches SET patch_digest = %s WHERE operation_id = %s",
                               ("a" * 64, pins["operation_id"]))
    dispatch, effect, reservation, observations = _persisted(admin, project, request["action_id"], pins["operation_id"])
    assert dispatch["patch_digest"] == pins["patch_digest"]
    assert effect["exposure_held"] is True and reservation["state"] == "reserved" and observations == []


def test_prepare_binds_one_held_effect_and_same_pin_replay(cpu_dispatch):
    admin, runtime, client, project, people, tokens = cpu_dispatch
    request, _, _ = _make_reservation(runtime, people, project)
    pins = _pins(request["action_id"], "operation-" + uuid4().hex)

    prepared = _prepare(client, project, tokens["owner"], pins)
    assert prepared.status_code == 200 and prepared.json()["state"] == "prepared"
    assert prepared.json()["physical_dispatch_authorized"] is False
    assert prepared.json()["launch_authorization_committed"] is False
    assert _prepare(client, project, tokens["owner"], pins).json() == prepared.json()

    changed = {**pins, "patch_digest": "a" * 64}
    conflict = _prepare(client, project, tokens["owner"], changed)
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "idempotency_conflict"
    dispatch, effect, reservation, observations = _persisted(admin, project, request["action_id"], pins["operation_id"])
    assert dispatch["state"] == "prepared"
    assert effect["state"] == "unknown" and effect["exposure_held"] is True
    assert reservation["state"] == "reserved" and observations == []


def test_prepare_journal_failure_rolls_back_dispatch_and_effect(cpu_dispatch, monkeypatch):
    admin, runtime, client, project, people, tokens = cpu_dispatch
    request, _, _ = _make_reservation(runtime, people, project)
    pins = _pins(request["action_id"], "operation-" + uuid4().hex)
    original = runtime._cpu_event

    def fail_after_journal(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected dispatch journal failure")

    monkeypatch.setattr(runtime, "_cpu_event", fail_after_journal)
    with pytest.raises(RuntimeError, match="injected dispatch journal failure"):
        _prepare(client, project, tokens["owner"], pins)
    dispatch, effect, reservation, observations = _persisted(admin, project, request["action_id"], pins["operation_id"])
    assert dispatch is None and effect is None and observations == []
    assert reservation["state"] == "reserved"


def test_begin_rollback_then_replay_never_reauthorizes_start(cpu_dispatch, monkeypatch):
    _, runtime, client, project, people, tokens = cpu_dispatch
    request, _, _ = _make_reservation(runtime, people, project)
    pins = _pins(request["action_id"], "operation-" + uuid4().hex)
    assert _prepare(client, project, tokens["owner"], pins).status_code == 200
    path = f"/api/v1/projects/{project}/cpu-worker-dispatches/{pins['operation_id']}/begin"
    original = runtime._cpu_event

    def fail_after_journal(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected begin journal failure")

    monkeypatch.setattr(runtime, "_cpu_event", fail_after_journal)
    with pytest.raises(RuntimeError, match="injected begin journal failure"):
        client.post(path, headers=_headers(tokens["owner"]))
    monkeypatch.setattr(runtime, "_cpu_event", original)
    current = client.get(f"/api/v1/projects/{project}/cpu-worker-dispatches/{pins['operation_id']}",
                         headers=_headers(tokens["owner"]))
    assert current.status_code == 200 and current.json()["state"] == "prepared"
    first = client.post(path, headers=_headers(tokens["owner"]))
    replay = client.post(path, headers=_headers(tokens["owner"]))
    assert first.status_code == replay.status_code == 200
    assert first.json()["start_once"] is True
    assert replay.json()["start_once"] is False
    assert first.json()["state"] == replay.json()["state"] == "launch-intent"


def test_begin_fails_closed_without_shared_usage_guard(monkeypatch):
    import builtins

    original_import = builtins.__import__
    def deny_usage_import(name, *args, **kwargs):
        if name in {"skybuild.task_usage", "task_usage"}:
            raise ModuleNotFoundError(name)
        return original_import(name, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.delitem(sys.modules, "skybuild.task_usage", raising=False)
        scoped.setattr(builtins, "__import__", deny_usage_import)
        with pytest.raises(DomainError) as error:
            CPUWorkerDispatch._require_usage_clear(object(), "project", "task")
    assert error.value.code == "usage_guard_unavailable" and error.value.status_code == 503


def test_late_unresolved_usage_blocks_one_shot_launch(cpu_dispatch, monkeypatch):
    admin, runtime, client, project, people, tokens = cpu_dispatch
    request, _, _ = _make_reservation(runtime, people, project)
    pins = _pins(request["action_id"], "operation-" + uuid4().hex)
    assert _prepare(client, project, tokens["owner"], pins).status_code == 200
    guard = sys.modules["skybuild.task_usage"]
    monkeypatch.setattr(guard, "unresolved_usage_exists", lambda _connection, _project, _task: True)
    path = f"/api/v1/projects/{project}/cpu-worker-dispatches/{pins['operation_id']}/begin"
    blocked = client.post(path, headers=_headers(tokens["owner"]))
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "usage_conflict"
    dispatch, effect, reservation, observations = _persisted(
        admin, project, request["action_id"], pins["operation_id"])
    assert dispatch["state"] == "prepared"
    assert effect["exposure_held"] is True and reservation["state"] == "reserved"
    assert observations == []


def test_prepare_requires_live_exact_claim_fence_and_rolls_back(cpu_dispatch):
    admin, runtime, client, project, people, tokens = cpu_dispatch
    request, _, _ = _make_reservation(runtime, people, project, lease_seconds=1)
    with runtime._connection() as connection:
        connection.execute("SELECT pg_sleep(1.1)")
    pins = _pins(request["action_id"], "operation-" + uuid4().hex)
    response = _prepare(client, project, tokens["owner"], pins)
    assert response.status_code == 409 and response.json()["error"]["code"] == "claim_conflict"
    dispatch, effect, reservation, observations = _persisted(admin, project, request["action_id"], pins["operation_id"])
    assert dispatch is None and effect is None and observations == []
    assert reservation["state"] == "reserved"


def test_concurrent_competing_prepare_has_one_dispatch_and_one_effect(cpu_dispatch):
    admin, runtime, _, project, people, _ = cpu_dispatch
    request, _, _ = _make_reservation(runtime, people, project)
    pin_sets = [_pins(request["action_id"], "operation-" + uuid4().hex) for _ in range(2)]
    barrier = Barrier(2)

    def prepare(pins):
        barrier.wait()
        try:
            return CPUWorkerDispatch(runtime).prepare(people["owner"], project, pins["action_id"], pins["operation_id"],
                **{key: datetime.fromisoformat(value) if key == "approved_until" else value
                   for key, value in pins.items() if key not in {"action_id", "operation_id"}})
        except DomainError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(prepare, pin_sets))
    winners = [result for result in results if isinstance(result, dict)]
    losers = [result for result in results if isinstance(result, DomainError)]
    assert len(winners) == len(losers) == 1 and losers[0].code == "idempotency_conflict"
    winner = next(pins for pins, result in zip(pin_sets, results) if isinstance(result, dict))
    dispatch, effect, reservation, observations = _persisted(admin, project, request["action_id"], winner["operation_id"])
    assert dispatch["state"] == "prepared" and effect["exposure_held"] is True
    assert reservation["state"] == "reserved" and observations == []
    with admin._connection() as connection:
        assert connection.execute("SELECT count(*) AS n FROM cpu_worker_dispatches WHERE action_id = %s",
                                  (request["action_id"],)).fetchone()["n"] == 1
        assert connection.execute("SELECT count(*) AS n FROM task_effects WHERE project_id = %s AND task_id = %s",
                                  (project, request["task_id"])).fetchone()["n"] == 1


def test_invocation_and_terminal_observation_identity_is_exact_and_monotonic(cpu_dispatch):
    admin, runtime, client, project, people, tokens = cpu_dispatch
    request, _, _ = _make_reservation(runtime, people, project)
    pins = _pins(request["action_id"], "operation-" + uuid4().hex)
    assert _prepare(client, project, tokens["owner"], pins).status_code == 200
    base = f"/api/v1/projects/{project}/cpu-worker-dispatches/{pins['operation_id']}"
    assert client.post(base + "/begin", headers=_headers(tokens["owner"])).status_code == 200
    invocation = _unit_identity(pins)
    invocation_response = client.post(base + "/invocation", headers=_headers(tokens["owner"]), json=invocation)
    assert invocation_response.status_code == 200 and invocation_response.json()["state"] == "running"

    for mismatch in ({**invocation, "host_id": "other-host"},
                     {**invocation, "unit_name": "skybuild-job-" + "0" * 24 + ".service"},
                     {**invocation, "launch_nonce": "0" * 32},
                     {**invocation, "invocation_id": "b" * 32}):
        body = {**mismatch, "observation_id": str(uuid4()), "phase": "running"}
        response = client.post(base + "/observations", headers=_headers(tokens["owner"]), json=body)
        assert response.status_code == 409

    terminal_id = str(uuid4())
    completed = _settleable_observation(pins, terminal_id)
    response = client.post(base + "/observations", headers=_headers(tokens["owner"]), json=completed)
    assert response.status_code == 200 and response.json()["state"] == "terminal"
    assert client.post(base + "/observations", headers=_headers(tokens["owner"]), json=completed).json() == response.json()
    mutated = {**completed, "worker_result_digest": "e" * 64}
    replay = client.post(base + "/observations", headers=_headers(tokens["owner"]), json=mutated)
    assert replay.status_code == 409 and replay.json()["error"]["code"] == "idempotency_conflict"
    later_running = {**_unit_identity(pins), "observation_id": str(uuid4()), "phase": "running"}
    blocked = client.post(base + "/observations", headers=_headers(tokens["owner"]), json=later_running)
    assert blocked.status_code == 409
    dispatch, effect, reservation, observations = _persisted(admin, project, request["action_id"], pins["operation_id"])
    assert dispatch["state"] == "terminal" and effect["exposure_held"] is True
    assert reservation["state"] == "reserved" and [row["phase"] for row in observations] == ["completed"]


def test_unknown_observation_keeps_capacity_held_and_blocks_settlement(cpu_dispatch):
    admin, runtime, client, project, people, tokens = cpu_dispatch
    request, _, _ = _make_reservation(runtime, people, project)
    pins = _pins(request["action_id"], "operation-" + uuid4().hex)
    assert _prepare(client, project, tokens["owner"], pins).status_code == 200
    base = f"/api/v1/projects/{project}/cpu-worker-dispatches/{pins['operation_id']}"
    assert client.post(base + "/begin", headers=_headers(tokens["owner"])).status_code == 200
    identity = _unit_identity(pins)
    assert client.post(base + "/invocation", headers=_headers(tokens["owner"]), json=identity).status_code == 200
    unknown = {**identity, "observation_id": str(uuid4()), "phase": "unknown"}
    assert client.post(base + "/observations", headers=_headers(tokens["owner"]), json=unknown).status_code == 200
    settle = client.post(base + "/settle", headers=_headers(tokens["owner"]), json={"observation_id": unknown["observation_id"]})
    assert settle.status_code == 409
    dispatch, effect, reservation, observations = _persisted(admin, project, request["action_id"], pins["operation_id"])
    assert dispatch["state"] == "unknown" and effect["state"] == "unknown" and effect["exposure_held"] is True
    assert reservation["state"] == "reserved" and observations[0]["phase"] == "unknown"


def test_held_submit_is_rejected_then_terminal_settlement_allows_owner_relay(cpu_dispatch):
    admin, runtime, client, project, people, tokens = cpu_dispatch
    request, _, _ = _make_reservation(runtime, people, project)
    pins = _pins(request["action_id"], "operation-" + uuid4().hex)
    assert _prepare(client, project, tokens["owner"], pins).status_code == 200
    base = f"/api/v1/projects/{project}/cpu-worker-dispatches/{pins['operation_id']}"
    assert client.post(base + "/begin", headers=_headers(tokens["owner"])).status_code == 200
    identity = _unit_identity(pins)
    assert client.post(base + "/invocation", headers=_headers(tokens["owner"]), json=identity).status_code == 200
    current = runtime.get_task(people["owner"], project, request["task_id"])
    token = Store.workflow_token(current)
    receipt = dict(source_head="a" * 40, target_base="b" * 40, source_branch="refs/heads/result",
                   attempt_id=token.attempt_id, claim_fence=token.claim_fence,
                   input_generation=token.input_generation, definition_revision=token.definition_revision,
                   policy_version=token.policy_version)
    # Worker result submission cannot cross the held effect/reservation boundary.
    with pytest.raises(DomainError) as blocked:
        runtime.workflow_transition(people["worker"], project, request["task_id"], "submit",
                                    receipt, current["revision"], "manual-result-relay-once")
    assert blocked.value.code in {"effect_conflict", "capacity_conflict", "workflow_conflict"}
    assert Store.workflow_token(runtime.get_task(people["owner"], project, request["task_id"])).place.value == "working"

    terminal_id = str(uuid4())
    completed = _settleable_observation(pins, terminal_id)
    assert client.post(base + "/observations", headers=_headers(tokens["owner"]), json=completed).status_code == 200
    settle_body = {"observation_id": terminal_id}
    first = client.post(base + "/settle", headers=_headers(tokens["owner"]), json=settle_body)
    assert first.status_code == 200 and first.json()["state"] == "settled"
    # Trusted owner relay uses same original task receipt after exact terminal proof.
    latest = runtime.get_task(people["owner"], project, request["task_id"])
    submitted = runtime.workflow_transition(people["owner"], project, request["task_id"], "submit",
                                            receipt, latest["revision"], "manual-result-relay-once")
    assert Store.workflow_token(submitted["task"]).place.value == "validating"
    replayed = runtime.workflow_transition(people["owner"], project, request["task_id"], "submit",
                                           receipt, latest["revision"], "manual-result-relay-once")
    assert Store.workflow_token(replayed["task"]).place.value == "validating"
    retry = client.post(base + "/settle", headers=_headers(tokens["owner"]), json=settle_body)
    assert retry.status_code == 200 and retry.json()["state"] == "settled"
    dispatch, effect, reservation, observations = _persisted(admin, project, request["action_id"], pins["operation_id"])
    assert dispatch["state"] == "settled"
    assert effect["state"] == "settled" and effect["exposure_held"] is False
    assert reservation["state"] == "released" and len(observations) == 1
    with admin._connection() as connection:
        assert connection.execute("SELECT count(*) AS n FROM effect_journal WHERE operation_id = %s AND action = 'settled'",
                                  (pins["operation_id"],)).fetchone()["n"] == 1


def test_nonowner_cannot_prepare_or_read_dispatch(cpu_dispatch):
    _, runtime, client, project, people, tokens = cpu_dispatch
    request, _, _ = _make_reservation(runtime, people, project)
    pins = _pins(request["action_id"], "operation-" + uuid4().hex)
    denied = _prepare(client, project, tokens["worker"], pins)
    assert denied.status_code == 403
    path = f"/api/v1/projects/{project}/cpu-worker-dispatches/{pins['operation_id']}"
    assert client.get(path, headers=_headers(tokens["outsider"])).status_code == 403
    with runtime._connection() as connection:
        assert connection.execute("SELECT count(*) AS n FROM task_effects WHERE operation_id = %s",
                                  (pins["operation_id"],)).fetchone()["n"] == 0
