"""Store-only CPU reservations against task-owned disposable PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4
from pathlib import Path

import psycopg
import pytest

from skybuild.contracts import DomainError
from test_store import actors, seed_api_authority, store
from test_claims import ready, expire
import skybuild

assert Path(skybuild.__file__).resolve().parents[2] == Path(__file__).resolve().parents[1]


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


@pytest.mark.parametrize('operation', ['reserve_cpu', 'explain_cpu'])
def test_lease_expiring_during_checks_rolls_back(store, actors, monkeypatch, operation):
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
    if operation == 'reserve_cpu':
        failure('claim_conflict', lambda: store.reserve_cpu(people['worker'], project, **request))
    else:
        result = store.explain_cpu(people['worker'], project, **request)
        assert result['eligible'] is False and result['reasons'][0]['code'] == 'claim_conflict'
        assert result['reasons'][0]['message'] == 'Ownership lease expired before CPU reservation'
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


def cpu_record_counts(store, project):
    with store._connection() as connection:
        result = {table: connection.execute(f'SELECT count(*) AS n FROM {table} WHERE project_id = %s',
                                         (project,)).fetchone()['n']
                for table in ('cpu_reservations', 'cpu_journal', 'idempotency', 'task_journal', 'claim_journal', 'task_effects')}
        result['mutable_records'] = {
            table: connection.execute(f'SELECT to_jsonb(t) AS row FROM {table} t WHERE project_id = %s ORDER BY to_jsonb(t)::text',
                                      (project,)).fetchall()
            for table in ('cpu_pools', 'cpu_reservations', 'tasks', 'task_readiness', 'task_claims', 'task_effects')}
        return result


def test_explanation_is_unredeemable_read_only_and_preserves_digest(store, actors):
    import hashlib
    from skybuild.store import _json
    project, people = actors
    request = setup(store, people, project)
    before = cpu_record_counts(store, project)
    result = store.explain_cpu(people['worker'], project, **request)
    assert result['eligible'] is True and result['outcome'] == 'eligible'
    assert result['physical_dispatch_authorized'] is False and result['snapshot_only'] is True
    assert result['reasons'] == [] and result['reservation_state'] is None
    assert result['held_units'] == 0 and result['pool']['capacity'] == 1
    assert cpu_record_counts(store, project) == before
    reserved = store.reserve_cpu(people['worker'], project, **request)
    original_payload = dict(project_id=project, task_id=request['task_id'], actor=people['worker'].principal_id,
                            action_id=request['action_id'], attempt_id=request['attempt_id'], units=request['units'],
                            task_revision=request['expected_revision'], readiness_generation=request['readiness_generation'],
                            claim_fence=request['claim_fence'], generation=request['generation'], local_generation=request['local_generation'])
    assert reserved['intent_hash'] == hashlib.sha256(_json(original_payload).encode()).hexdigest()


@pytest.mark.parametrize('boundary,code', [
    ('central-disabled', 'control_conflict'), ('local-disabled', 'control_conflict'),
    ('central-generation', 'control_conflict'), ('local-generation', 'control_conflict'),
    ('revision', 'stale_revision'), ('fence', 'claim_conflict'), ('holder', 'claim_conflict'),
    ('expired', 'claim_conflict'), ('readiness-generation', 'workflow_conflict'),
    ('readiness-assessment', 'workflow_conflict'), ('dependency', 'workflow_conflict'),
    ('unknown-effect', 'effect_conflict'), ('held-task', 'capacity_conflict'),
    ('held-pool', 'capacity_conflict'), ('attempt-identity', 'idempotency_conflict'),
    ('action-identity', 'idempotency_conflict'),
])
def test_explanation_and_reservation_share_denials(store, actors, boundary, code):
    project, people = actors
    request = setup(store, people, project, capacity=2 if boundary == 'attempt-identity' else 1, actor='owner')
    actor = people['owner']
    if boundary == 'central-disabled': store.configure_cpu_pool(actor, project, 1, False, 1, 'disable', reason='Test stop')
    elif boundary == 'local-disabled': store.set_cpu_local_control(actor, project, False, 2, 'disable', reason='Test stop')
    elif boundary == 'central-generation': request['generation'] = 99
    elif boundary == 'local-generation': request['local_generation'] = 99
    elif boundary == 'revision': request['expected_revision'] = 1
    elif boundary == 'fence': request['claim_fence'] = 99
    elif boundary == 'holder': actor = people['peer']
    elif boundary == 'expired': expire(store, project, request['task_id'])
    elif boundary == 'readiness-generation': request['readiness_generation'] = 99
    elif boundary == 'readiness-assessment':
        with store._connection() as connection:
            connection.execute('UPDATE task_readiness SET assessed_generation = 0 WHERE project_id = %s AND task_id = %s', (project, request['task_id']))
    elif boundary == 'dependency':
        # Seed an inconsistent cached-ready record to exercise the defensive
        # current-dependency predicate, independently of readiness invalidation.
        dependency = ready(store, people, project, 'unfinished-dependency')
        with store._connection() as connection:
            connection.execute('INSERT INTO task_dependencies VALUES (%s, %s, %s)', (project, request['task_id'], dependency['task_id']))
    elif boundary == 'unknown-effect':
        body = dict(operation_id=uuid4().hex, attempt_id=request['attempt_id'], authority_epoch=1,
                    authority_generation=1, input_digest='a' * 64, policy_digest='b' * 64,
                    allocation_refs=[request['action_id']])
        effect = store.create_effect_intent(actor, project, request['task_id'], body, 2, 'effect', claim_fence=1)
        store.observe_effect(actor, project, effect['operation_id'], 'unknown', 'Lost reply', 'unknown', claim_fence=1)
    elif boundary in {'held-task', 'action-identity'}:
        store.reserve_cpu(actor, project, **request)
        if boundary == 'held-task': request.update(action_id=uuid4().hex, attempt_id=uuid4().hex)
        else: request['units'] = 2
    elif boundary in {'held-pool', 'attempt-identity'}:
        other = setup(store, people, project, 'other-held', actor='owner')
        store.reserve_cpu(actor, project, **other)
        if boundary == 'attempt-identity': request['attempt_id'] = other['attempt_id']
    before = cpu_record_counts(store, project)
    explanation = store.explain_cpu(actor, project, **request)
    assert explanation['eligible'] is False and explanation['outcome'] == 'denied'
    assert len(explanation['reasons']) == 1 and explanation['reasons'][0]['code'] == code
    assert explanation['physical_dispatch_authorized'] is False
    assert cpu_record_counts(store, project) == before
    with pytest.raises(DomainError) as caught:
        store.reserve_cpu(actor, project, **request)
    assert explanation['reasons'][0] == dict(code=caught.value.code, message=caught.value.message,
                                           status_code=caught.value.status_code)
    assert cpu_record_counts(store, project) == before


def test_absent_controls_are_denied_without_inventing_capacity(store, actors):
    project, people = actors
    request = dict(task_id='missing', action_id=uuid4().hex, attempt_id=uuid4().hex, units=1,
                   expected_revision=1, readiness_generation=1, claim_fence=1, generation=1, local_generation=1)
    result = store.explain_cpu(people['worker'], project, **request)
    assert result['pool'] is None and result['held_units'] is None
    assert result['reasons'][0]['code'] == 'control_conflict'
    failure('control_conflict', lambda: store.reserve_cpu(people['worker'], project, **request))


def test_replay_reports_held_or_cancelled_never_fresh_eligibility(store, actors):
    project, people = actors
    request = setup(store, people, project)
    store.reserve_cpu(people['worker'], project, **request)
    for expected_state in ('reserved', 'cancelled'):
        if expected_state == 'cancelled':
            store.cancel_cpu_reservation(people['owner'], project, request['action_id'], reason='No dispatch')
            expire(store, project, request['task_id'])
            store.configure_cpu_pool(people['owner'], project, 1, False, 1, 'pause', reason='Pause')
        before = cpu_record_counts(store, project)
        result = store.explain_cpu(people['worker'], project, **request)
        assert result['outcome'] == 'replay' and result['eligible'] is False
        assert result['reservation_state'] == expected_state and result['physical_dispatch_authorized'] is False
        assert result['pool'] is None and result['held_units'] is None
        assert store.reserve_cpu(people['worker'], project, **request)['state'] == expected_state
        assert cpu_record_counts(store, project) == before


def test_explanation_current_auth_and_markdown_authority_precede_replay(store, actors):
    project, people = actors
    request = setup(store, people, project)
    store.reserve_cpu(people['worker'], project, **request)
    failure('authorization', lambda: store.explain_cpu(people['outsider'], project, **request))
    with store._connection() as connection:
        connection.execute("DELETE FROM principal_grants WHERE principal_id = %s AND operation = 'tasks:claim'", (people['worker'].principal_id,))
    failure('authorization', lambda: store.explain_cpu(people['worker'], project, **request))
    with store._connection() as connection:
        connection.execute("INSERT INTO ledger_imports (project_id, commit_id, content_sha256, import_sha256, task_count, status_counts, authority) VALUES (%s, 'commit', %s, %s, 1, '{}'::jsonb, 'markdown')", (project, 'c' * 64, 'd' * 64))
    failure('authority', lambda: store.explain_cpu(people['owner'], project, **request))
    failure('authority', lambda: store.reserve_cpu(people['owner'], project, **request))


@pytest.mark.parametrize('change', ['control', 'lease'])
def test_explanation_does_not_authorize_later_changed_state(store, actors, change):
    project, people = actors
    request = setup(store, people, project)
    assert store.explain_cpu(people['worker'], project, **request)['eligible'] is True
    if change == 'control':
        store.set_cpu_local_control(people['owner'], project, False, 2, 'stop-after-read', reason='Stop')
    else: expire(store, project, request['task_id'])
    failure('control_conflict' if change == 'control' else 'claim_conflict',
            lambda: store.reserve_cpu(people['worker'], project, **request))


def test_explanation_snapshot_survives_concurrent_control_change(store, actors, monkeypatch):
    project, people = actors
    request = setup(store, people, project)
    original = store._require_current_dependencies
    def stop_after_pool_read(connection, scope, task):
        original(connection, scope, task)
        store.set_cpu_local_control(people['owner'], project, False, 2, 'concurrent-stop', reason='Stop')
    monkeypatch.setattr(store, '_require_current_dependencies', stop_after_pool_read)
    result = store.explain_cpu(people['worker'], project, **request)
    assert result['eligible'] is True and result['pool']['local_enabled'] is True
    assert store_cpu_pool(store, project)['local_enabled'] is False
    failure('control_conflict', lambda: store.reserve_cpu(people['worker'], project, **request))


def test_explanation_database_failure_remains_unavailable(store, actors, monkeypatch):
    project, people = actors
    request = setup(store, people, project)
    def unavailable(*args, **kwargs):
        raise psycopg.OperationalError('Injected database failure')
    monkeypatch.setattr(store, '_cpu_eligibility', unavailable)
    failure('unavailable', lambda: store.explain_cpu(people['worker'], project, **request))
