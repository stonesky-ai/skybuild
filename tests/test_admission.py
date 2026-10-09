"""Store-only CPU reservations against task-owned disposable PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import psycopg
import pytest

from skybuild.contracts import DomainError
from test_store import actors, seed_api_authority, store
from test_claims import ready, expire


def setup(store, people, project, task_id='cpu-task', capacity=1, actor='worker'):
    task = ready(store, people, project, task_id)
    store.claim_task(people[actor], project, task_id, task['revision'], 'claim-' + task_id)
    with store._connection() as connection:
        generation = connection.execute('SELECT input_generation FROM task_readiness WHERE project_id = %s AND task_id = %s', (project, task_id)).fetchone()['input_generation']
    if not store_cpu_pool(store, project):
        store.configure_cpu_pool(people['owner'], project, capacity, True, 0, 'pool', reason='Test pool')
        store.set_cpu_local_control(people['owner'], project, True, 1, 'local', reason='Test local control')
    return dict(task_id=task_id, action_id=uuid4().hex, attempt_id=uuid4().hex, units=1,
                expected_revision=task['revision'], readiness_generation=generation,
                claim_fence=1, generation=1, local_generation=2)


def store_cpu_pool(store, project):
    with store._connection() as connection:
        return connection.execute('SELECT * FROM cpu_pools WHERE project_id = %s', (project,)).fetchone()


def failure(code, action):
    with pytest.raises(DomainError) as caught:
        action()
    assert caught.value.code == code


def test_atomic_capacity_and_explicit_cancel_replay(store, actors):
    project, people = actors
    requests = [setup(store, people, project, task_id) for task_id in ('cpu-a', 'cpu-b')]
    barrier = Barrier(2)
    def reserve(request):
        barrier.wait()
        try:
            return store.reserve_cpu(people['worker'], project, **request)
        except DomainError as error:
            return error
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, requests))
    assert sum(isinstance(result, dict) for result in results) == 1
    winner = next(result for result in results if isinstance(result, dict))
    assert next(result for result in results if isinstance(result, DomainError)).code == 'capacity_conflict'
    request = next(request for request in requests if request['action_id'] == winner['action_id'])
    failure('capacity_conflict', lambda: store.release_claim(people['worker'], project, request['task_id'], 1, 2, 'release', reason='Not cancellation'))
    expire(store, project, request['task_id'])
    failure('capacity_conflict', lambda: store.reconcile_claim(people['owner'], project, request['task_id'], 1, 2, 'reconcile', reason='Expired'))
    failure('claim_conflict', lambda: store.cancel_cpu_reservation(people['worker'], project, winner['action_id'], reason='Expired worker'))
    cancelled = store.cancel_cpu_reservation(people['owner'], project, winner['action_id'], reason='No dispatch exists')
    assert cancelled['state'] == 'cancelled'
    assert store.reserve_cpu(people['worker'], project, **request) == cancelled
    assert store.cancel_cpu_reservation(people['owner'], project, winner['action_id'], reason='Retry') == cancelled
    other = next(request for request in requests if request['action_id'] != winner['action_id'])
    assert store.reserve_cpu(people['worker'], project, **other)['state'] == 'reserved'


def test_control_cas_composition_and_restart(store, actors):
    project, people = actors
    request = setup(store, people, project)
    owner = people['owner']
    failure('authorization', lambda: store.set_cpu_local_control(people['worker'], project, False, 2, 'bad', reason='Not owner'))
    store.set_cpu_local_control(owner, project, False, 2, 'stop-local', reason='Stop')
    store.configure_cpu_pool(owner, project, 2, True, 1, 'enable-central', reason='Central cannot clear local')
    request['generation'] = 2
    request['local_generation'] = 3
    failure('control_conflict', lambda: store.reserve_cpu(people['worker'], project, **request))
    failure('stale_revision', lambda: store.set_cpu_local_control(owner, project, True, 2, 'stale-enable', reason='Delayed enable'))
    from skybuild.store import Store
    restarted = Store(store.dsn, store.expected_database)
    assert store_cpu_pool(restarted, project)['local_enabled'] is False
    store.set_cpu_local_control(owner, project, True, 3, 'enable-local', reason='Current enable')
    request['local_generation'] = 4
    assert restarted.reserve_cpu(people['worker'], project, **request)['state'] == 'reserved'
    failure('capacity_conflict', lambda: store.configure_cpu_pool(owner, project, 0, False, 2, 'shrink', reason='Cannot discard exposure'))


def test_revision_fence_identity_authorization_and_validation(store, actors):
    project, people = actors
    request = setup(store, people, project)
    for field, value, code in [('expected_revision', 1, 'stale_revision'), ('readiness_generation', 99, 'workflow_conflict'),
                               ('claim_fence', 2, 'claim_conflict'), ('units', True, 'validation'),
                               ('units', 0, 'validation'), ('generation', 2, 'control_conflict')]:
        failure(code, lambda: store.reserve_cpu(people['worker'], project, **{**request, field: value}))
    failure('authorization', lambda: store.reserve_cpu(people['outsider'], project, **request))
    result = store.reserve_cpu(people['worker'], project, **request)
    assert store.reserve_cpu(people['worker'], project, **request) == result
    failure('idempotency_conflict', lambda: store.reserve_cpu(people['worker'], project, **{**request, 'units': 2}))
    failure('idempotency_conflict', lambda: store.reserve_cpu(people['peer'], project, **request))
    failure('authorization', lambda: store.cancel_cpu_reservation(people['peer'], project, request['action_id'], reason='Wrong actor'))
    with store._connection() as connection:
        assert connection.execute("SELECT count(*) AS n FROM cpu_journal WHERE project_id = %s AND action = 'reserve'", (project,)).fetchone()['n'] == 1


def test_effect_uncertainty_retains_units_and_journal_immutable(store, actors):
    project, people = actors
    request = setup(store, people, project, actor='owner')
    store.reserve_cpu(people['owner'], project, **request)
    body = dict(operation_id=uuid4().hex, attempt_id=request['attempt_id'], authority_epoch=1, authority_generation=1,
                input_digest='a' * 64, policy_digest='b' * 64, allocation_refs=[request['action_id']])
    effect = store.create_effect_intent(people['owner'], project, request['task_id'], body, 2, 'intent', claim_fence=1)
    store.observe_effect(people['owner'], project, effect['operation_id'], 'unknown', 'Lost reply', 'unknown', claim_fence=1)
    failure('effect_conflict', lambda: store.cancel_cpu_reservation(people['owner'], project, request['action_id'], reason='Unknown cannot release'))
    for statement in ('UPDATE cpu_journal SET reason = reason', 'DELETE FROM cpu_journal', 'TRUNCATE cpu_journal',
                      'DELETE FROM cpu_reservations', 'TRUNCATE cpu_reservations', 'UPDATE cpu_reservations SET units = units + 1',
                      'DELETE FROM cpu_pools', 'TRUNCATE cpu_pools', 'UPDATE cpu_pools SET generation = generation'):
        with pytest.raises(DomainError) as caught:
            with store._connection() as connection:
                connection.execute(statement)
        assert isinstance(caught.value.__cause__, psycopg.Error)


def test_journal_failure_rolls_back_reservation_and_capacity(store, actors, monkeypatch):
    project, people = actors
    request = setup(store, people, project)
    original = store._cpu_event
    def fail(*args):
        raise RuntimeError('Injected journal failure')
    monkeypatch.setattr(store, '_cpu_event', fail)
    with pytest.raises(RuntimeError):
        store.reserve_cpu(people['worker'], project, **request)
    with store._connection() as connection:
        assert not connection.execute('SELECT 1 FROM cpu_reservations WHERE action_id = %s', (request['action_id'],)).fetchone()
    monkeypatch.setattr(store, '_cpu_event', original)
    assert store.reserve_cpu(people['worker'], project, **request)['state'] == 'reserved'


def test_stop_racing_reservation_serializes(store, actors):
    project, people = actors
    request = setup(store, people, project)
    barrier = Barrier(2)
    def action(name):
        barrier.wait()
        try:
            if name == 'stop':
                return store.configure_cpu_pool(people['owner'], project, 1, False, 1, 'stop', reason='Stop admission')
            return store.reserve_cpu(people['worker'], project, **request)
        except DomainError as error:
            return error
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(action, ['stop', 'reserve']))
    assert results[0]['enabled'] is False
    assert isinstance(results[1], dict) or results[1].code == 'control_conflict'
    failure('control_conflict', lambda: store.reserve_cpu(people['worker'], project, **{**request, 'action_id': uuid4().hex, 'attempt_id': uuid4().hex}))


def test_lease_expiring_during_checks_rolls_back(store, actors, monkeypatch):
    project, people = actors
    request = setup(store, people, project)
    with store._connection() as connection:
        connection.execute("UPDATE task_claims SET lease_until = clock_timestamp() + interval '1 second', claim_revision = claim_revision + 1 WHERE project_id = %s AND task_id = %s", (project, request['task_id']))
    original = store._require_current_dependencies
    def delayed(connection, project_id, task):
        original(connection, project_id, task)
        # Let actual clock time expire after the early lease check.
        connection.execute('SELECT pg_sleep(1.1)')
    monkeypatch.setattr(store, '_require_current_dependencies', delayed)
    failure('claim_conflict', lambda: store.reserve_cpu(people['worker'], project, **request))
    with store._connection() as connection:
        assert not connection.execute('SELECT 1 FROM cpu_reservations WHERE action_id = %s', (request['action_id'],)).fetchone()
        assert connection.execute("SELECT count(*) AS n FROM cpu_journal WHERE project_id = %s AND action = 'reserve'", (project,)).fetchone()['n'] == 0


def test_integer_units_global_attempt_and_cancel_race(store, actors):
    project, people = actors
    first = setup(store, people, project, 'multi-unit', capacity=3)
    first['units'] = 2
    second = setup(store, people, project, 'single-unit')
    store.reserve_cpu(people['worker'], project, **first)
    failure('idempotency_conflict', lambda: store.reserve_cpu(people['worker'], project, **{**second, 'attempt_id': first['attempt_id']}))
    second['units'] = 2
    failure('capacity_conflict', lambda: store.reserve_cpu(people['worker'], project, **second))
    second['units'] = 1
    store.reserve_cpu(people['worker'], project, **second)
    barrier = Barrier(2)
    def cancel_or_replay(name):
        barrier.wait()
        if name == 'cancel':
            return store.cancel_cpu_reservation(people['worker'], project, first['action_id'], reason='Never dispatched')
        return store.reserve_cpu(people['worker'], project, **first)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(cancel_or_replay, ['cancel', 'replay']))
    assert results[0]['state'] == 'cancelled'
    assert results[1]['state'] in {'reserved', 'cancelled'}
    assert store.reserve_cpu(people['worker'], project, **first)['state'] == 'cancelled'
    with store._connection() as connection:
        assert connection.execute("SELECT sum(units) AS units FROM cpu_reservations WHERE project_id = %s AND state = 'reserved'", (project,)).fetchone()['units'] == 1


def test_markdown_authority_and_revoked_grants_block_replays(store, actors):
    project, people = actors
    request = setup(store, people, project)
    store.reserve_cpu(people['worker'], project, **request)
    with store._connection() as connection:
        connection.execute('DELETE FROM principal_grants WHERE principal_id = %s AND operation = %s', (people['worker'].principal_id, 'tasks:claim'))
    failure('authorization', lambda: store.reserve_cpu(people['worker'], project, **request))
    failure('authorization', lambda: store.cancel_cpu_reservation(people['worker'], project, request['action_id'], reason='Revoked'))
    with store._connection() as connection:
        connection.execute("UPDATE ledger_imports SET authority = 'markdown' WHERE project_id = %s", (project,))
    failure('authority', lambda: store.configure_cpu_pool(people['owner'], project, 2, True, 1, 'pool-again', reason='No cutover'))
    failure('authority', lambda: store.cancel_cpu_reservation(people['owner'], project, request['action_id'], reason='No cutover'))


def test_control_and_cancellation_journal_failures_roll_back(store, actors, monkeypatch):
    project, people = actors
    request = setup(store, people, project)
    store.reserve_cpu(people['worker'], project, **request)
    def fail(*args):
        raise RuntimeError('Injected journal failure')
    monkeypatch.setattr(store, '_cpu_event', fail)
    with pytest.raises(RuntimeError):
        store.configure_cpu_pool(people['owner'], project, 1, False, 1, 'stop-rollback', reason='Stop')
    assert store_cpu_pool(store, project)['enabled'] is True
    assert store_cpu_pool(store, project)['generation'] == 1
    with pytest.raises(RuntimeError):
        store.cancel_cpu_reservation(people['worker'], project, request['action_id'], reason='Cancel')
    assert store.reserve_cpu(people['worker'], project, **request)['state'] == 'reserved'


def test_attempt_identity_serializes_across_project_pools(store, actors):
    from skybuild.store import OPERATIONS
    project, people = actors
    other_project = project + '-other'
    seed_api_authority(store, other_project)
    store.provision_principal(people['worker'].principal_id, people['worker_token'],
                              grants={project: OPERATIONS, other_project: OPERATIONS})
    first = setup(store, people, project)
    second = setup(store, people, other_project)
    second['attempt_id'] = first['attempt_id']
    barrier = Barrier(2)
    def reserve(pair):
        scope, request = pair
        barrier.wait()
        try:
            return store.reserve_cpu(people['worker'], scope, **request)
        except DomainError as error:
            return error
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, [(project, first), (other_project, second)]))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert next(result for result in results if isinstance(result, DomainError)).code == 'idempotency_conflict'
