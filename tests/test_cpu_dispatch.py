"""Crash, replay, fencing and terminal-proof checks for the launch-free simulator."""
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import psycopg
import pytest

from skybuild.contracts import DomainError
from skybuild.cpu_dispatch import CPUDispatch
from skybuild.store import Store
from test_admission import setup
from test_claims import expire
from test_observations import packet
from test_store import actors, error, store


def prepared(store, people, project, *, actor='worker', task_id='fake-cpu'):
    request = setup(store, people, project, task_id, actor=actor)
    store.reserve_cpu(people[actor], project, **request)
    runner = CPUDispatch(store)
    operation = uuid4().hex
    result = runner.prepare_fake(people['owner'], project, request['action_id'], operation,
                                 input_digest='a' * 64)
    return runner, request, operation, result


def persisted(store, operation):
    with store._connection() as connection:
        effect = connection.execute('SELECT * FROM task_effects WHERE operation_id = %s', (operation,)).fetchone()
        reservation = connection.execute('SELECT r.* FROM cpu_reservations r JOIN cpu_fake_dispatches d '
                                         'USING (action_id) WHERE d.operation_id = %s', (operation,)).fetchone()
        return effect, reservation


def assert_held(store, operation):
    effect, reservation = persisted(store, operation)
    assert effect['state'] == 'unknown' and effect['exposure_held'] is True
    assert reservation['state'] == 'reserved'


def stop_and_settle(runner, owner, project, operation):
    runner.request_fake_stop(owner, project, operation)
    runner.acknowledge_fake_stop(owner, project, operation)
    return runner.reconcile_fake(owner, project, operation)


def test_crash_boundaries_and_restart_preserve_one_identity(store, actors):
    project, people = actors
    owner = people['owner']
    runner, request, operation, intent = prepared(store, people, project)
    assert intent['receipt'] is None
    assert intent['physical_dispatch_authorized'] is False
    assert_held(store, operation)
    # Crash after intent commit but before simulator I/O.
    restarted = CPUDispatch(Store(store.dsn, store.expected_database))
    assert restarted.prepare_fake(owner, project, request['action_id'], operation, input_digest='a' * 64) == intent
    restarted.start_fake(owner, project, operation)  # Its reply is deliberately discarded.
    restarted = CPUDispatch(Store(store.dsn, store.expected_database))
    running = restarted.start_fake(owner, project, operation)
    assert running['receipt']['starts'] == 1
    assert running['process_identity'] == intent['process_identity']
    assert restarted.reconcile_fake(owner, project, operation)['state'] == 'unknown'
    assert_held(store, operation)
    pending = restarted.request_fake_stop(owner, project, operation)
    assert pending['state'] == 'stop-pending'
    assert pending['receipt']['state'] == 'running'
    assert_held(store, operation)
    restarted.acknowledge_fake_stop(owner, project, operation)  # Lose the stop acknowledgment.
    assert_held(store, operation)
    settled = CPUDispatch(Store(store.dsn, store.expected_database)).reconcile_fake(owner, project, operation)
    assert settled['state'] == 'settled'
    assert settled['receipt']['state'] == 'terminal'
    assert settled['receipt']['starts'] == 1
    assert restarted.reconcile_fake(owner, project, operation) == settled
    assert restarted.start_fake(owner, project, operation) == settled
    effect, reservation = persisted(store, operation)
    assert effect['state'] == 'settled' and effect['exposure_held'] is False
    assert reservation['state'] == 'released'
    assert store.get_task(owner, project, request['task_id'])['status'] == 'ready'
    assert store.reserve_cpu(people['worker'], project, **request)['state'] == 'released'
    with store._connection() as connection:
        assert connection.execute("SELECT count(*) AS n FROM effect_journal WHERE operation_id = %s AND action = 'settled'",
                                  (operation,)).fetchone()['n'] == 1


def test_stop_before_start_tombstone_fences_delayed_start(store, actors):
    project, people = actors
    runner, request, operation, _ = prepared(store, people, project)
    runner.request_fake_stop(people['owner'], project, operation)
    assert runner.start_fake(people['owner'], project, operation)['receipt'] is None
    terminal = stop_and_settle(runner, people['owner'], project, operation)
    assert terminal['receipt']['starts'] == 0
    assert runner.start_fake(people['owner'], project, operation) == terminal


def test_concurrent_prepare_start_and_stop_keep_one_operation(store, actors):
    project, people = actors
    request = setup(store, people, project)
    store.reserve_cpu(people['worker'], project, **request)
    runner, operation = CPUDispatch(store), uuid4().hex
    owner = people['owner']
    def prepare(_):
        return runner.prepare_fake(owner, project, request['action_id'], operation, input_digest='a' * 64)
    with ThreadPoolExecutor(max_workers=4) as executor:
        intents = list(executor.map(prepare, range(4)))
    assert all(intent == intents[0] for intent in intents)
    calls = [runner.start_fake, runner.start_fake, runner.request_fake_stop, runner.request_fake_stop]
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda method: method(owner, project, operation), calls))
    result = stop_and_settle(runner, owner, project, operation)
    assert result['receipt']['starts'] in (0, 1)
    with store._connection() as connection:
        assert connection.execute('SELECT count(*) AS n FROM cpu_fake_receipts WHERE operation_id = %s',
                                  (operation,)).fetchone()['n'] == 1


@pytest.mark.parametrize('boundary', ['prepare', 'start'])
@pytest.mark.parametrize('change,code', [('central', 'control_conflict'), ('local', 'control_conflict'),
                                       ('reenable', 'control_conflict'), ('claim', 'claim_conflict'),
                                       ('readiness', 'workflow_conflict'), ('revision', 'stale_revision')])
def test_each_binding_revalidated_at_prepare_and_start(store, actors, boundary, change, code):
    project, people = actors
    owner = people['owner']
    runner = CPUDispatch(store)
    request = setup(store, people, project)
    store.reserve_cpu(people['worker'], project, **request)
    operation = uuid4().hex
    if boundary == 'start':
        runner.prepare_fake(owner, project, request['action_id'], operation, input_digest='a' * 64)
    if change == 'central':
        store.configure_cpu_pool(owner, project, 1, False, 1, 'disable', reason='Stop')
    elif change in ('local', 'reenable'):
        store.set_cpu_local_control(owner, project, False, 2, 'disable', reason='Stop')
        if change == 'reenable':
            store.set_cpu_local_control(owner, project, True, 3, 'enable', reason='Fresh generation')
    elif change == 'claim':
        expire(store, project, request['task_id'])
    elif change == 'readiness':
        with store._connection() as connection:
            connection.execute('UPDATE task_readiness SET input_generation = input_generation + 1 '
                               'WHERE project_id = %s AND task_id = %s', (project, request['task_id']))
    else:
        error('capacity_conflict', lambda: store.update_task(
            owner, project, request['task_id'], {'description': 'Changed inputs'},
            request['expected_revision'], 'edit'))
        # Existing Store fences semantic edits while capacity is held. Inject a
        # stale restored snapshot to exercise the independent dispatch check.
        with store._connection() as connection:
            connection.execute('UPDATE tasks SET revision = revision + 1 WHERE project_id = %s AND task_id = %s',
                               (project, request['task_id']))
    action = (lambda: runner.start_fake(owner, project, operation)) if boundary == 'start' else (
        lambda: runner.prepare_fake(owner, project, request['action_id'], operation, input_digest='a' * 64))
    error(code, action)
    if boundary == 'start':
        assert_held(store, operation)
        # Restrictions fence starts, but cannot prevent safe evidence reconciliation.
        assert stop_and_settle(runner, owner, project, operation)['state'] == 'settled'


def test_observation_exit_and_expired_claim_do_not_settle(store, actors):
    project, people = actors
    runner, request, operation, _ = prepared(store, people, project)
    runner.start_fake(people['owner'], project, operation)
    expire(store, project, request['task_id'])
    body = {**packet(request), 'state': 'exited'}
    store.record_observation(people['worker'], project, body)
    assert runner.reconcile_fake(people['owner'], project, operation)['state'] == 'unknown'
    assert_held(store, operation)
    error('effect_conflict', lambda: store.cancel_cpu_reservation(people['owner'], project,
                                                                 request['action_id'], reason='Not proof'))
    error('capacity_conflict', lambda: store.reconcile_claim(people['owner'], project,
                                                             request['task_id'], 1, 2, 'release', reason='Not proof'))


def test_authorization_identity_and_generic_unknown_remain_closed(store, actors):
    project, people = actors
    runner, request, operation, _ = prepared(store, people, project, actor='owner')
    for method in (runner.start_fake, runner.request_fake_stop, runner.acknowledge_fake_stop, runner.reconcile_fake):
        error('authorization', lambda: method(people['worker'], project, operation))
    error('authorization', lambda: runner.prepare_fake(people['worker'], project, request['action_id'],
                                                       operation, input_digest='a' * 64))
    error('idempotency_conflict', lambda: runner.prepare_fake(people['owner'], project, request['action_id'],
                                                              uuid4().hex, input_digest='a' * 64))
    error('idempotency_conflict', lambda: runner.prepare_fake(people['owner'], project, request['action_id'],
                                                              operation, input_digest='b' * 64))
    error('effect_conflict', lambda: runner.acknowledge_fake_stop(people['owner'], project, operation))
    error('effect_conflict', lambda: store.observe_effect(people['owner'], project, operation, 'cancelled',
                                                         'Not proof', 'cancel-effect', claim_fence=1))
    error('validation', lambda: store.observe_effect(people['owner'], project, operation, 'settled',
                                                    'Not proof', 'settle-effect', claim_fence=1))
    # A generic unknown effect with matching-looking fields cannot become fake.
    stop_and_settle(runner, people['owner'], project, operation)
    other = setup(store, people, project, 'generic', actor='owner')
    store.reserve_cpu(people['owner'], project, **other)
    generic = uuid4().hex
    store.create_effect_intent(people['owner'], project, other['task_id'],
        dict(operation_id=generic, attempt_id=other['attempt_id'], authority_epoch=1, authority_generation=1,
             input_digest='a' * 64, policy_digest='b' * 64, allocation_refs=[other['action_id']]),
        2, 'generic-intent', claim_fence=1)
    store.observe_effect(people['owner'], project, generic, 'unknown', 'Lost reply', 'unknown', claim_fence=1)
    error('not_found', lambda: runner.reconcile_fake(people['owner'], project, generic))
    with pytest.raises(DomainError):
        with store._connection() as connection:
            connection.execute("UPDATE task_effects SET state = 'settled', exposure_held = false WHERE operation_id = %s", (generic,))
    with pytest.raises(DomainError):
        with store._connection() as connection:
            connection.execute('INSERT INTO cpu_fake_dispatches (operation_id, action_id, reservation_hash, process_identity) '
                               'SELECT %s, action_id, intent_hash, %s FROM cpu_reservations WHERE action_id = %s',
                               (generic, uuid4(), other['action_id']))


def test_foreign_proof_and_direct_release_rejected_by_database(store, actors):
    project, people = actors
    runner, request, operation, _ = prepared(store, people, project)
    statements = [
        ("UPDATE cpu_reservations SET state = 'released' WHERE action_id = %s", (request['action_id'],)),
        ("UPDATE cpu_reservations SET state = 'cancelled' WHERE action_id = %s", (request['action_id'],)),
        ("UPDATE task_effects SET state = 'settled', exposure_held = false WHERE operation_id = %s", (operation,)),
        ("UPDATE cpu_fake_dispatches SET state = 'settled' WHERE operation_id = %s", (operation,)),
        ('INSERT INTO cpu_fake_receipts (operation_id, process_identity, reservation_hash, state, starts) '
         "VALUES (%s, %s, %s, 'terminal', 0)", (operation, uuid4(), 'wrong-binding')),
        ('DELETE FROM cpu_fake_dispatches WHERE operation_id = %s', (operation,)),
    ]
    for query, parameters in statements:
        with pytest.raises(DomainError) as caught:
            with store._connection() as connection:
                connection.execute(query, parameters)
        assert isinstance(caught.value.__cause__, psycopg.Error)
    runner.start_fake(people['owner'], project, operation)
    with pytest.raises(DomainError):
        with store._connection() as connection:
            connection.execute('UPDATE cpu_fake_receipts SET process_identity = %s WHERE operation_id = %s',
                               (uuid4(), operation))
    assert_held(store, operation)


def test_two_running_operations_keep_separate_capacity_and_terminal_proofs(store, actors):
    project, people = actors
    runner, owner = CPUDispatch(store), people['owner']
    operations = []
    for task_id in ('parallel-first', 'parallel-second'):
        request = setup(store, people, project, task_id, capacity=2)
        store.reserve_cpu(people['worker'], project, **request)
        operation = uuid4().hex
        runner.prepare_fake(owner, project, request['action_id'], operation, input_digest='a' * 64)
        operations.append(operation)
    first, second = operations
    with ThreadPoolExecutor(max_workers=2) as executor:
        started = list(executor.map(lambda operation: runner.start_fake(owner, project, operation), operations))
    assert all(result['receipt']['state'] == 'running' for result in started)
    assert all(result['receipt']['starts'] == 1 for result in started)
    assert_held(store, first)
    assert_held(store, second)

    terminal = stop_and_settle(runner, owner, project, first)
    first_proof = terminal['receipt']
    assert first_proof['state'] == 'terminal'
    assert persisted(store, first)[1]['state'] == 'released'
    assert runner.reconcile_fake(owner, project, first) == terminal
    assert runner.reconcile_fake(owner, project, second)['receipt'] == started[1]['receipt']
    assert_held(store, second)
    with store._connection() as connection:
        assert connection.execute("SELECT sum(units) AS held FROM cpu_reservations "
                                  "WHERE project_id = %s AND state = 'reserved'", (project,)).fetchone()['held'] == 1

    # Even a genuine terminal proof from another operation cannot be transplanted.
    # Commit the second stop intent first so rejection reaches identity validation,
    # rather than merely detecting the absence of a stop request.
    runner.request_fake_stop(owner, project, second)
    with pytest.raises(DomainError) as caught:
        with store._connection() as connection:
            connection.execute("UPDATE cpu_fake_receipts SET process_identity = %s, "
                               "reservation_hash = %s, state = 'terminal' WHERE operation_id = %s",
                               (first_proof['process_identity'], first_proof['reservation_hash'], second))
    assert isinstance(caught.value.__cause__, psycopg.errors.RaiseException)
    assert 'Only a requested fake stop may create terminal proof' in str(caught.value.__cause__)
    pending = runner.reconcile_fake(owner, project, second)
    assert pending['state'] == 'stop-pending'
    assert pending['receipt'] == started[1]['receipt']
    assert_held(store, second)


@pytest.mark.parametrize('boundary', ['prepare', 'start', 'stop', 'ack', 'settle'])
def test_journal_failure_rolls_back_entire_transition(store, actors, monkeypatch, boundary):
    project, people = actors
    runner = CPUDispatch(store)
    request = setup(store, people, project)
    store.reserve_cpu(people['worker'], project, **request)
    operation, owner = uuid4().hex, people['owner']
    actions = [
        ('prepare', lambda: runner.prepare_fake(owner, project, request['action_id'], operation, input_digest='a' * 64)),
        ('start', lambda: runner.start_fake(owner, project, operation)),
        ('stop', lambda: runner.request_fake_stop(owner, project, operation)),
        ('ack', lambda: runner.acknowledge_fake_stop(owner, project, operation)),
        ('settle', lambda: runner.reconcile_fake(owner, project, operation)),
    ]
    for name, action in actions:
        if name == boundary:
            def crash(*args):
                raise RuntimeError('Simulated crash before commit')
            with monkeypatch.context() as patch:
                patch.setattr(store, '_cpu_event', crash)
                with pytest.raises(RuntimeError):
                    action()
            if boundary == 'prepare':
                with store._connection() as connection:
                    assert not connection.execute('SELECT 1 FROM task_effects WHERE operation_id = %s', (operation,)).fetchone()
            else:
                assert_held(store, operation)
        action()
    assert persisted(store, operation)[1]['state'] == 'released'
