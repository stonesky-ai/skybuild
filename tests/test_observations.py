"""Evidence receipt against task-owned disposable PostgreSQL only."""
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import psycopg
import pytest

from skybuild.observations import FakeObserver
from skybuild.store import Store
from test_store import actors, store, error
from test_admission import setup
from test_claims import expire


def reserved(store, people, project, actor="worker"):
    request = setup(store, people, project, actor=actor)
    store.reserve_cpu(people[actor], project, **request)
    return request


def packet(reservation, **changes):
    return dict(event_id=uuid4().hex, task_id=reservation['task_id'],
                attempt_id=reservation['attempt_id'], claim_fence=reservation['claim_fence'],
                component_id='fake-component', source_id='fake-observer', boot_id='boot-1',
                pid=12, process_start='boot-monotonic-100', source_sequence=1,
                observed_at='2026-10-08T10:00:00+00:00', state='running',
                evidence_refs=['artifact-1'], **changes)


def changed(body, **changes):
    return dict(body, **changes)


def journal(store, project):
    with store._connection() as connection:
        return connection.execute('SELECT * FROM observation_events WHERE project_id = %s ORDER BY source_sequence', (project,)).fetchall()


def test_replay_reordering_restart_and_identity_pinning(store, actors):
    project, people = actors
    reservation = reserved(store, people, project)
    first = packet(reservation)
    later = changed(first, event_id=uuid4().hex, source_sequence=3, state='waiting')
    old = changed(first, event_id=uuid4().hex, source_sequence=2)
    observer = FakeObserver([first, later, old])
    result = observer.replay(store, people['worker'], project)
    restarted = Store(store.dsn, store.expected_database)
    assert observer.replay(restarted, people['worker'], project) == result
    assert len(journal(store, project)) == 3
    assert store.observation_status(people['worker'], project, first['task_id'])[0]['event_id'] == later['event_id']
    for change in ({'boot_id': 'boot-2'}, {'pid': 13}, {'process_start': 'boot-monotonic-200'}):
        different = changed(first, event_id=uuid4().hex, source_sequence=4, **change)
        store.record_observation(people['worker'], project, different)
    assert len(journal(store, project)) == 6
    assert store.observation_status(people['worker'], project, first['task_id'])[0]['event_id'] == later['event_id']
    error('idempotency_conflict', lambda: store.record_observation(people['worker'], project, changed(first, state='exited')))
    error('idempotency_conflict', lambda: store.record_observation(people['worker'], project, changed(first, event_id=uuid4().hex)))


def test_concurrent_identical_and_conflicting_replay(store, actors):
    project, people = actors
    first = packet(reserved(store, people, project))
    def record(body):
        return store.record_observation(people['worker'], project, body)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(record, [first] * 4))
    assert all(result == results[0] for result in results)
    assert len(journal(store, project)) == 1
    error('idempotency_conflict', lambda: record(changed(first, evidence_refs=['different'])))
    newer = [changed(first, event_id=uuid4().hex, source_sequence=n) for n in range(2, 9)]
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(record, reversed(newer)))
    assert store.observation_status(people['worker'], project, first['task_id'])[0]['source_sequence'] == 8


def test_evidence_never_releases_or_completes_and_stale_fence_is_history(store, actors):
    project, people = actors
    reservation = reserved(store, people, project)
    first = packet(reservation)
    with store._connection() as connection:
        before = connection.execute('SELECT * FROM tasks WHERE project_id = %s', (project,)).fetchone()
        claim_before = connection.execute('SELECT * FROM task_claims WHERE project_id = %s', (project,)).fetchone()
        reserved_before = connection.execute('SELECT * FROM cpu_reservations WHERE project_id = %s', (project,)).fetchone()
    expire(store, project, first['task_id'])
    for n, state in enumerate(('unknown', 'exited', 'interrupted', 'result-reported'), 1):
        store.record_observation(people['worker'], project, changed(first, event_id=uuid4().hex, source_sequence=n, state=state))
    with store._connection() as connection:
        assert connection.execute('SELECT * FROM tasks WHERE project_id = %s', (project,)).fetchone() == before
        held = connection.execute('SELECT * FROM task_claims WHERE project_id = %s', (project,)).fetchone()
        assert held['held'] and held['fence'] == claim_before['fence']
        assert connection.execute('SELECT * FROM cpu_reservations WHERE project_id = %s', (project,)).fetchone() == reserved_before
        assert connection.execute('SELECT count(*) AS n FROM task_effects WHERE project_id = %s', (project,)).fetchone()['n'] == 0
    # Owner reconciliation is a separate explicit operation, never inferred from evidence.
    error('capacity_conflict', lambda: store.reconcile_claim(people['owner'], project, first['task_id'], 1, held['claim_revision'], 'blocked', reason='Evidence is not release'))
    store.cancel_cpu_reservation(people['owner'], project, reservation['action_id'], reason='Fixture confirms never dispatched')
    store.reconcile_claim(people['owner'], project, first['task_id'], 1, held['claim_revision'], 'reconcile', reason='Fixture asserts no process was launched')
    stale = changed(first, event_id=uuid4().hex, source_sequence=10)
    store.record_observation(people['worker'], project, stale)
    assert store.observation_status(people['worker'], project, first['task_id'])[0]['source_sequence'] == 4
    assert len(journal(store, project)) == 5


def test_auth_validation_and_append_only(store, actors):
    project, people = actors
    first = packet(reserved(store, people, project))
    for actor in ('peer', 'outsider'):
        error('authorization', lambda: store.record_observation(people[actor], project, first))
    error('authorization', lambda: store.observation_status(people['outsider'], project, first['task_id']))
    for changes in ({'source_sequence': True}, {'pid': 0}, {'observed_at': '2026-10-08'},
                    {'state': 'done'}, {'evidence_refs': ['x'] * 9}, {'evidence_refs': ['/secret']},
                    {'unknown': 'x'}, {'boot_id': None}):
        error('validation', lambda: store.record_observation(people['worker'], project, changed(first, **changes)))
    error('observation_identity', lambda: store.record_observation(people['worker'], project, changed(first, claim_fence=2)))
    error('observation_identity', lambda: store.record_observation(people['worker'], project, changed(first, attempt_id='other')))
    store.record_observation(people['worker'], project, first)
    for mutation in ('UPDATE observation_events SET state = state', 'DELETE FROM observation_events', 'TRUNCATE observation_events CASCADE'):
        with psycopg.connect(store.dsn) as connection:
            connection.execute('SET search_path TO skybuild, pg_catalog')
            with pytest.raises(psycopg.errors.RaiseException):
                connection.execute(mutation)


def test_transaction_crash_rolls_back_journal_and_projection(store, actors, monkeypatch):
    from contextlib import contextmanager
    project, people = actors
    first = packet(reserved(store, people, project))
    connection_factory = store._connection

    @contextmanager
    def crash_before_commit():
        with connection_factory() as connection:
            yield connection
            raise RuntimeError('Simulated receipt crash before commit')

    with monkeypatch.context() as patch:
        patch.setattr(store, '_connection', crash_before_commit)
        with pytest.raises(RuntimeError, match='Simulated receipt crash'):
            store.record_observation(people['worker'], project, first)
    assert journal(store, project) == []
    assert store.observation_status(people['worker'], project, first['task_id']) == []
    store.record_observation(people['worker'], project, first)
    assert len(journal(store, project)) == 1
    assert store.observation_status(people['worker'], project, first['task_id'])[0]['event_id'] == first['event_id']


def test_result_evidence_keeps_effect_exposure(store, actors):
    project, people = actors
    reservation = reserved(store, people, project, actor="owner")
    body = dict(operation_id=uuid4().hex, attempt_id=reservation['attempt_id'],
                authority_epoch=1, authority_generation=1, input_digest='a' * 64,
                policy_digest='b' * 64, allocation_refs=[reservation['action_id']])
    effect = store.create_effect_intent(people['owner'], project, reservation['task_id'], body,
                                       reservation['expected_revision'], 'effect', claim_fence=1)
    store.record_observation(people['owner'], project, changed(packet(reservation), state='result-reported'))
    with store._connection() as connection:
        after = connection.execute('SELECT * FROM task_effects WHERE operation_id = %s', (body['operation_id'],)).fetchone()
    assert after['state'] == effect['state']
    assert after['exposure_held'] == effect['exposure_held']
