"""Durable CPU dispatch simulator; no commands, callbacks, network or OS launch.

These owner-only methods exercise the transaction and recovery contract. The
fake adapter is a separate PostgreSQL transaction, not a physical executor.
Reservations and ordinary effect records grant no dispatch authority.
"""

import hashlib
import re
from uuid import uuid4

from .contracts import DomainError
from .store import _identifier, _json, _public


KIND = 'cpu-fake-v1'
POLICY_DIGEST = hashlib.sha256(b'skybuild:cpu-fake-v1:no-external-effects').hexdigest()


class CPUDispatch:
    """Explicit fake-only coordinator over the existing Store transactions."""

    def __init__(self, store):
        self.store = store

    def _lock(self, connection, principal, project_id):
        principal = self.store._authorize_effect_writer(connection, principal, project_id)
        self.store._require_api_authority(connection, project_id)
        self.store._graph_lock(connection, project_id)
        self.store._cpu_lock(connection, project_id)
        return principal

    def _load(self, connection, project_id, operation_id):
        _identifier(operation_id, 'operation_id')
        dispatch = connection.execute(
            'SELECT d.* FROM cpu_fake_dispatches d JOIN task_effects e USING (operation_id) '
            'WHERE d.operation_id = %s AND e.project_id = %s FOR UPDATE OF d',
            (operation_id, project_id)).fetchone()
        if not dispatch:
            raise DomainError('not_found', 'Fake dispatch not found', 404)
        return dispatch

    @staticmethod
    def _result(connection, dispatch):
        proof = connection.execute('SELECT * FROM cpu_fake_receipts WHERE operation_id = %s',
                                   (dispatch['operation_id'],)).fetchone()
        return _public({**dispatch, 'adapter_kind': KIND, 'physical_dispatch_authorized': False,
                        'receipt': proof})

    def _current_reservation(self, connection, reservation):
        """Revalidate immutable reservation bindings under graph and pool locks."""
        project, task_id = reservation['project_id'], reservation['task_id']
        if reservation['state'] != 'reserved':
            raise DomainError('capacity_conflict', 'CPU reservation is not held', 409)
        pool = connection.execute('SELECT * FROM cpu_pools WHERE project_id = %s FOR UPDATE',
                                  (project,)).fetchone()
        if (not pool or not pool['enabled'] or not pool['local_enabled'] or
                pool['generation'] != reservation['generation'] or
                pool['local_generation'] != reservation['local_generation']):
            raise DomainError('control_conflict', 'CPU controls are disabled or changed', 409)
        task = self.store._task(connection, project, task_id, lock=True)
        if task['revision'] != reservation['task_revision']:
            raise DomainError('stale_revision', 'Task revision has changed', 409)
        readiness = connection.execute('SELECT * FROM task_readiness WHERE project_id = %s AND task_id = %s',
                                       (project, task_id)).fetchone()
        if (task['status'] != 'ready' or not readiness or
                readiness['input_generation'] != reservation['readiness_generation'] or
                readiness['assessed_generation'] != reservation['readiness_generation']):
            raise DomainError('workflow_conflict', 'CPU dispatch requires current readiness', 409)
        self.store._require_current_dependencies(connection, project, task)
        actor = self.store._principal(connection, reservation['actor'])
        self.store._authorize(connection, actor, project, 'tasks:claim')
        self.store._require_claim_fence(connection, actor, project, task_id, reservation['claim_fence'])
        claim = connection.execute('SELECT task_revision FROM task_claims WHERE project_id = %s AND task_id = %s',
                                   (project, task_id)).fetchone()
        if claim['task_revision'] != reservation['task_revision']:
            raise DomainError('claim_conflict', 'Claim revision does not match reservation', 409)

    @staticmethod
    def _still_live(connection, reservation):
        # Check at publication, after potentially slow validation or lock waits.
        return connection.execute(
            'SELECT 1 FROM task_claims WHERE project_id = %s AND task_id = %s '
            'AND held AND holder = %s AND fence = %s AND task_revision = %s '
            'AND lease_until > clock_timestamp()',
            (reservation['project_id'], reservation['task_id'], reservation['actor'],
             reservation['claim_fence'], reservation['task_revision'])).fetchone()

    def prepare_fake(self, principal, project_id, action_id, operation_id, *, input_digest):
        """Commit uncertain fake intent before any simulator call; replay never launches."""
        _identifier(action_id, 'action_id')
        _identifier(operation_id, 'operation_id')
        if not isinstance(input_digest, str) or not re.fullmatch('[0-9a-f]{64}', input_digest):
            raise DomainError('validation', 'input_digest must be a SHA-256 hex digest', 422)
        with self.store._connection() as connection:
            principal = self._lock(connection, principal, project_id)
            connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))',
                               ('skybuild:cpu-action:' + action_id,))
            connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))',
                               ('skybuild:effect:' + operation_id,))
            reservation = connection.execute('SELECT * FROM cpu_reservations WHERE action_id = %s '
                                             'AND project_id = %s FOR UPDATE', (action_id, project_id)).fetchone()
            if not reservation:
                raise DomainError('not_found', 'CPU reservation not found', 404)
            prior = connection.execute('SELECT d.*, e.input_digest FROM cpu_fake_dispatches d '
                                       'JOIN task_effects e USING (operation_id) '
                                       'WHERE d.action_id = %s', (action_id,)).fetchone()
            if prior:
                if prior['operation_id'] != operation_id or prior['input_digest'] != input_digest:
                    raise DomainError('idempotency_conflict', 'CPU action already binds another fake intent', 409)
                return self._result(connection, self._load(connection, project_id, operation_id))
            if connection.execute('SELECT 1 FROM task_effects WHERE operation_id = %s', (operation_id,)).fetchone():
                raise DomainError('idempotency_conflict', 'Operation already belongs to another effect', 409)
            self._current_reservation(connection, reservation)
            if connection.execute('SELECT 1 FROM task_effects WHERE project_id = %s AND task_id = %s '
                                  'AND exposure_held', (project_id, reservation['task_id'])).fetchone():
                raise DomainError('effect_conflict', 'Task already has unresolved effect exposure', 409)
            intent = dict(adapter_kind=KIND, operation_id=operation_id, action_id=action_id,
                          reservation_hash=reservation['intent_hash'], input_digest=input_digest,
                          policy_digest=POLICY_DIGEST)
            digest = hashlib.sha256(_json(intent).encode()).hexdigest()
            connection.execute(
                'INSERT INTO task_effects (operation_id, project_id, task_id, attempt_id, task_revision, '
                'authority_epoch, authority_generation, input_digest, policy_digest, allocation_refs, '
                'intent_hash, adapter_kind, state) VALUES (%s, %s, %s, %s, %s, 1, %s, %s, %s, %s, %s, %s, %s)',
                (operation_id, project_id, reservation['task_id'], reservation['attempt_id'],
                 reservation['task_revision'], reservation['generation'], input_digest, POLICY_DIGEST,
                 [action_id], digest, KIND, 'unknown'))
            connection.execute('INSERT INTO cpu_fake_dispatches '
                               '(operation_id, action_id, reservation_hash, process_identity) VALUES (%s, %s, %s, %s)',
                               (operation_id, action_id, reservation['intent_hash'], uuid4()))
            if not self._still_live(connection, reservation):
                raise DomainError('claim_conflict', 'Claim expired before fake intent publication', 409)
            effect = _public(connection.execute('SELECT * FROM task_effects WHERE operation_id = %s',
                                                (operation_id,)).fetchone())
            self.store._effect_journal(connection, principal, effect, None,
                                       'Fake dispatch intent committed; no physical launch authorized')
            dispatch = self._load(connection, project_id, operation_id)
            self.store._cpu_event(connection, principal, project_id, 'fake-intent',
                                  'Hold capacity before simulator I/O', None, dispatch)
            return self._result(connection, dispatch)

    def start_fake(self, principal, project_id, operation_id):
        """Simulate at most one start in a new transaction; never execute a command."""
        with self.store._connection() as connection:
            principal = self._lock(connection, principal, project_id)
            dispatch = self._load(connection, project_id, operation_id)
            # Receipt presence or a stop tombstone makes retries observational.
            result = self._result(connection, dispatch)
            if dispatch['state'] != 'unknown' or result['receipt']:
                return result
            reservation = connection.execute('SELECT * FROM cpu_reservations WHERE action_id = %s FOR UPDATE',
                                             (dispatch['action_id'],)).fetchone()
            self._current_reservation(connection, reservation)
            connection.execute('INSERT INTO cpu_fake_receipts '
                               '(operation_id, process_identity, reservation_hash, state, starts) '
                               'VALUES (%s, %s, %s, %s, 1)',
                               (operation_id, dispatch['process_identity'], dispatch['reservation_hash'], 'running'))
            if not self._still_live(connection, reservation):
                raise DomainError('claim_conflict', 'Claim expired before fake start publication', 409)
            after = self._result(connection, dispatch)
            self.store._cpu_event(connection, principal, project_id, 'fake-start',
                                  'Simulator start recorded; capacity remains held', result, after)
            return after

    def request_fake_stop(self, principal, project_id, operation_id):
        """Durably fence delayed starts; this is not a stop acknowledgment."""
        with self.store._connection() as connection:
            principal = self._lock(connection, principal, project_id)
            before = self._load(connection, project_id, operation_id)
            if before['state'] == 'unknown':
                connection.execute("UPDATE cpu_fake_dispatches SET state = 'stop-pending' WHERE operation_id = %s",
                                   (operation_id,))
                after = self._load(connection, project_id, operation_id)
                self.store._cpu_event(connection, principal, project_id, 'fake-stop-request',
                                      'Stop requested; acknowledgment and reconciliation remain pending', before, after)
            return self._result(connection, self._load(connection, project_id, operation_id))

    def acknowledge_fake_stop(self, principal, project_id, operation_id):
        """The simulator commits an identity-bound terminal receipt, even before start."""
        with self.store._connection() as connection:
            principal = self._lock(connection, principal, project_id)
            dispatch = self._load(connection, project_id, operation_id)
            before = self._result(connection, dispatch)
            if dispatch['state'] == 'settled':
                return before
            if dispatch['state'] != 'stop-pending':
                raise DomainError('effect_conflict', 'Fake stop intent must commit before acknowledgment', 409)
            receipt = before['receipt']
            if receipt and receipt['state'] == 'terminal':
                return before
            if receipt:
                connection.execute("UPDATE cpu_fake_receipts SET state = 'terminal' WHERE operation_id = %s",
                                   (operation_id,))
            else:
                connection.execute('INSERT INTO cpu_fake_receipts '
                                   '(operation_id, process_identity, reservation_hash, state, starts) '
                                   'VALUES (%s, %s, %s, %s, 0)',
                                   (operation_id, dispatch['process_identity'], dispatch['reservation_hash'], 'terminal'))
            after = self._result(connection, dispatch)
            self.store._cpu_event(connection, principal, project_id, 'fake-stop-ack',
                                  'Simulator terminal proof recorded; capacity awaits reconciliation', before, after)
            return after

    def reconcile_fake(self, principal, project_id, operation_id):
        """Settle only the simulator's bound terminal proof; observations cannot settle."""
        with self.store._connection() as connection:
            principal = self._lock(connection, principal, project_id)
            dispatch = self._load(connection, project_id, operation_id)
            result = self._result(connection, dispatch)
            if dispatch['state'] == 'settled':
                return result
            receipt = result['receipt']
            if not receipt or receipt['state'] != 'terminal':
                return result
            if (receipt['process_identity'] != result['process_identity'] or
                    receipt['reservation_hash'] != result['reservation_hash']):
                raise DomainError('effect_conflict', 'Fake terminal proof identity mismatch', 409)
            before_effect = _public(connection.execute('SELECT * FROM task_effects WHERE operation_id = %s',
                                                       (operation_id,)).fetchone())
            connection.execute("UPDATE cpu_fake_dispatches SET state = 'settled' WHERE operation_id = %s",
                               (operation_id,))
            connection.execute("UPDATE task_effects SET state = 'settled', exposure_held = false WHERE operation_id = %s",
                               (operation_id,))
            connection.execute("UPDATE cpu_reservations SET state = 'released' WHERE action_id = %s",
                               (dispatch['action_id'],))
            after_effect = _public(connection.execute('SELECT * FROM task_effects WHERE operation_id = %s',
                                                      (operation_id,)).fetchone())
            self.store._effect_journal(connection, principal, after_effect, before_effect,
                                       'Identity-bound simulator terminal proof; no task completion accepted')
            after = self._result(connection, self._load(connection, project_id, operation_id))
            self.store._cpu_event(connection, principal, project_id, 'fake-settle',
                                  'Simulator proof atomically releases only its bound reservation', result, after)
            return after
