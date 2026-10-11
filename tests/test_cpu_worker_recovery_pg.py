"""Real PostgreSQL guards and HTTP recovery contract; no physical launch."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4
from threading import Barrier

from psycopg.types.json import Jsonb
from skybuild.contracts import DomainError

import psycopg
import pytest

from skybuild.cpu_worker_dispatch import CPUWorkerDispatch
from skybuild.cpu_worker_recovery import CPUWorkerRecovery
from test_cpu_worker_dispatch_pg import (
    cpu_dispatch, _headers, _make_reservation, _pins, _prepare, _persisted, _unit_identity,
)


def _case(env, *, lease_seconds=60):
    admin, runtime, client, project, people, tokens = env
    request, reservation, working = _make_reservation(runtime, people, project, lease_seconds=lease_seconds)
    pins = _pins(request['action_id'], uuid4().hex)
    assert _prepare(client, project, tokens['owner'], pins).status_code == 200
    base = f"/api/v1/projects/{project}/cpu-worker-dispatches/{pins['operation_id']}"
    assert client.post(base + '/begin', headers=_headers(tokens['owner'])).json()['start_once'] is True
    body = dict(recovery_id=str(uuid4()), expected_revision=working['revision'],
                claim_fence=request['claim_fence'], attempt_id=request['attempt_id'],
                reservation_hash=reservation['intent_hash'], proof=dict(
                    host_id=pins['host_id'], unit_name=pins['unit_name'], launch_nonce=pins['launch_nonce'],
                    controller_unit='skybuild-managed-controller-test.service',
                    controller_invocation_id='b' * 32, launcher_stopped=True,
                    launcher_cgroup_empty=True, launcher_fenced=True, unit_absent=True,
                    container_absent=True, manifest_absent=True, fence_sha256='c' * 64,
                    evidence_sha256='d' * 64, observed_at=datetime.now(timezone.utc).isoformat()))
    return pins, base, body, working


def test_owner_recovers_expired_attempt_and_exact_repeat_once(cpu_dispatch):
    admin, runtime, client, project, people, tokens = cpu_dispatch
    pins, base, body, working = _case(cpu_dispatch, lease_seconds=1)
    with admin._connection() as connection:
        connection.execute('SELECT pg_sleep(1.1)')
    reply = client.post(base + '/recover-unstarted', headers=_headers(tokens['owner']), json=body)
    assert reply.status_code == 200, reply.text
    assert reply.json()['state'] == 'cancelled'
    assert reply.json()['worker_success_accepted'] is False
    assert reply.json()['invocation_id'] is None
    assert reply.json()['latest_observation'] is None
    assert client.post(base + '/recover-unstarted', headers=_headers(tokens['owner']), json=body).json() == reply.json()
    assert client.get(base + '/recover-unstarted', headers=_headers(tokens['owner'])).json() == reply.json()
    assert runtime.get_task(people['owner'], project, working['task_id']) == working
    d, e, r, observations = _persisted(admin, project, pins['action_id'], pins['operation_id'])
    assert (d['state'], e['state'], e['exposure_held'], r['state'], observations) == (
        'cancelled', 'cancelled', False, 'cancelled', [])
    with admin._connection() as connection:
        assert connection.execute('SELECT count(*) AS n FROM cpu_worker_recoveries WHERE operation_id = %s',
                                  (pins['operation_id'],)).fetchone()['n'] == 1
        assert connection.execute("SELECT count(*) AS n FROM cpu_journal WHERE project_id = %s AND action = 'worker-unstarted-recovery'",
                                  (project,)).fetchone()['n'] == 1
    changed = deepcopy(body); changed['proof']['fence_sha256'] = 'e' * 64
    assert client.post(base + '/recover-unstarted', headers=_headers(tokens['owner']), json=changed).status_code == 409
    changed = {**body, 'recovery_id': str(uuid4())}
    assert client.post(base + '/recover-unstarted', headers=_headers(tokens['owner']), json=changed).status_code == 409


@pytest.mark.parametrize('field,value', [
    ('launcher_stopped', False), ('launcher_fenced', False), ('launcher_cgroup_empty', False),
    ('unit_absent', False), ('container_absent', False), ('manifest_absent', False),
    ('controller_invocation_id', '0' * 32), ('fence_sha256', 'missing'),
])
def test_contradictory_or_missing_proof_retains_every_exposure(cpu_dispatch, field, value):
    admin, runtime, client, project, people, tokens = cpu_dispatch
    pins, base, body, _ = _case(cpu_dispatch)
    before = _persisted(admin, project, pins['action_id'], pins['operation_id'])
    body['proof'][field] = value
    assert client.post(base + '/recover-unstarted', headers=_headers(tokens['owner']), json=body).status_code == 422
    assert _persisted(admin, project, pins['action_id'], pins['operation_id']) == before


@pytest.mark.parametrize('field,value', [('expected_revision', 999), ('claim_fence', 999),
                                       ('attempt_id', 'different'), ('reservation_hash', 'e' * 64)])
def test_stale_identity_cannot_release(cpu_dispatch, field, value):
    admin, runtime, client, project, people, tokens = cpu_dispatch
    pins, base, body, _ = _case(cpu_dispatch)
    before = _persisted(admin, project, pins['action_id'], pins['operation_id'])
    body[field] = value
    assert client.post(base + '/recover-unstarted', headers=_headers(tokens['owner']), json=body).status_code == 409
    assert _persisted(admin, project, pins['action_id'], pins['operation_id']) == before


def test_worker_cannot_recover_or_read_owner_receipt(cpu_dispatch):
    admin, runtime, client, project, people, tokens = cpu_dispatch
    pins, base, body, _ = _case(cpu_dispatch)
    assert client.post(base + '/recover-unstarted', headers=_headers(tokens['worker']), json=body).status_code == 403
    assert client.get(base + '/recover-unstarted', headers=_headers(tokens['worker'])).status_code == 403


def test_invocation_before_recovery_rejects_recovery(cpu_dispatch):
    admin, runtime, client, project, people, tokens = cpu_dispatch
    pins, base, body, _ = _case(cpu_dispatch)
    assert client.post(base + '/invocation', headers=_headers(tokens['owner']), json=_unit_identity(pins)).status_code == 200
    before = _persisted(admin, project, pins['action_id'], pins['operation_id'])
    assert client.post(base + '/recover-unstarted', headers=_headers(tokens['owner']), json=body).status_code == 409
    assert _persisted(admin, project, pins['action_id'], pins['operation_id']) == before


def test_recovery_rejects_late_invocation_and_never_regrants_start(cpu_dispatch):
    admin, runtime, client, project, people, tokens = cpu_dispatch
    pins, base, body, _ = _case(cpu_dispatch)
    assert client.post(base + '/recover-unstarted', headers=_headers(tokens['owner']), json=body).status_code == 200
    assert client.post(base + '/begin', headers=_headers(tokens['owner'])).json()['start_once'] is False
    assert client.post(base + '/invocation', headers=_headers(tokens['owner']), json=_unit_identity(pins)).status_code == 409
    for sql in [
        "UPDATE cpu_worker_dispatches SET state = 'running', invocation_id = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' WHERE operation_id = %s",
        "DELETE FROM cpu_worker_recoveries WHERE operation_id = %s",
        "UPDATE cpu_worker_recoveries SET request_digest = repeat('f',64) WHERE operation_id = %s",
    ]:
        with pytest.raises(psycopg.Error), admin._connection() as connection:
            connection.execute(sql, (pins['operation_id'],))
    with pytest.raises(psycopg.Error), admin._connection() as connection:
        connection.execute('TRUNCATE cpu_worker_recoveries')


def test_concurrent_exact_recovery_releases_once(cpu_dispatch):
    admin, runtime, client, project, people, tokens = cpu_dispatch
    pins, base, body, _ = _case(cpu_dispatch)
    def run():
        return CPUWorkerRecovery(runtime).recover(people['owner'], project, pins['operation_id'], **body)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert results[0] == results[1]
    with admin._connection() as connection:
        assert connection.execute('SELECT count(*) AS n FROM cpu_worker_recoveries WHERE operation_id = %s',
                                  (pins['operation_id'],)).fetchone()['n'] == 1


def test_sql_cannot_cancel_unproved_launch_intent(cpu_dispatch):
    admin, runtime, client, project, people, tokens = cpu_dispatch
    pins, base, body, _ = _case(cpu_dispatch)
    for sql, key in [
        ("UPDATE cpu_worker_dispatches SET state = 'cancelled' WHERE operation_id = %s", pins['operation_id']),
        ("UPDATE task_effects SET state = 'cancelled', exposure_held = false WHERE operation_id = %s", pins['operation_id']),
        ("UPDATE cpu_reservations SET state = 'cancelled' WHERE action_id = %s", pins['action_id']),
    ]:
        with pytest.raises(psycopg.Error), admin._connection() as connection:
            connection.execute(sql, (key,))


def test_concurrent_invocation_and_recovery_have_one_terminal_winner(cpu_dispatch):
    admin, runtime, client, project, people, tokens = cpu_dispatch
    pins, base, body, _ = _case(cpu_dispatch)
    barrier = Barrier(2)
    def recover():
        barrier.wait()
        try:
            return CPUWorkerRecovery(runtime).recover(people['owner'], project, pins['operation_id'], **body)['state']
        except DomainError as error:
            return error.code
    def invoke():
        barrier.wait()
        try:
            return CPUWorkerDispatch(runtime).record_invocation(
                people['owner'], project, pins['operation_id'], **_unit_identity(pins))['state']
        except DomainError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(recover), pool.submit(invoke)
        results = (a.result(), b.result())
    assert results in [('cancelled', 'effect_conflict'), ('effect_conflict', 'running')]
    d, e, r, _ = _persisted(admin, project, pins['action_id'], pins['operation_id'])
    if d['state'] == 'running':
        assert e['exposure_held'] and r['state'] == 'reserved'
    else:
        assert not e['exposure_held'] and r['state'] == 'cancelled'


def test_committed_exact_replay_does_not_require_new_host_observation(cpu_dispatch, monkeypatch):
    admin, runtime, client, project, people, tokens = cpu_dispatch
    pins, base, body, _ = _case(cpu_dispatch)
    first = client.post(base + '/recover-unstarted', headers=_headers(tokens['owner']), json=body)
    assert first.status_code == 200
    def stale(_proof):
        raise DomainError('effect_conflict', 'Old host proof', 409)
    monkeypatch.setattr('skybuild.cpu_worker_recovery.validate_proof', stale)
    assert client.post(base + '/recover-unstarted', headers=_headers(tokens['owner']), json=body).json() == first.json()


def test_database_rejects_contradictory_receipt(cpu_dispatch):
    admin, runtime, client, project, people, tokens = cpu_dispatch
    pins, base, body, _ = _case(cpu_dispatch)
    body['proof']['launcher_fenced'] = False
    with pytest.raises(psycopg.Error), admin._connection() as connection:
        connection.execute(
            'INSERT INTO cpu_worker_recoveries (recovery_id, operation_id, action_id, actor, request_digest, evidence) '
            'VALUES (%s, %s, %s, %s, %s, %s)',
            (body['recovery_id'], pins['operation_id'], pins['action_id'], people['owner'].principal_id,
             'f' * 64, Jsonb({key: value for key, value in body.items() if key != 'recovery_id'})))
