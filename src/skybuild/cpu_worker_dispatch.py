"""Owner-only natural-completion dispatch bound to the existing CPU ledger.

This coordinator records authorization and terminal evidence. The local
controller remains responsible for immutable input snapshots and for making a
single JobUnitManager call. No method requests or claims a physical stop.
"""

import hashlib
import re
from uuid import UUID, uuid4

from .contracts import DomainError
from .store import _identifier, _json, _public, _text


PROFILE = 'bounded-trusted-cpu-patch-v1'
POLICY_DIGEST = hashlib.sha256(b'skybuild:bounded-trusted-cpu-patch-v1:natural-completion').hexdigest()
_DIGEST = re.compile(r'[0-9a-f]{64}\Z')
_INVOCATION = re.compile(r'[0-9a-f]{32}\Z')
_UNIT = re.compile(r'skybuild-job-[0-9a-f]{24}\.service\Z')


class CPUWorkerDispatch:
    """Transactional dispatch records over `cpu_reservations`; no second ledger."""

    def __init__(self, store):
        self.store = store

    def _lock(self, connection, principal, project_id):
        principal = self.store._authorize_effect_writer(connection, principal, project_id)
        self.store._require_api_authority(connection, project_id)
        self.store._graph_lock(connection, project_id)
        self.store._cpu_lock(connection, project_id)
        return principal

    @staticmethod
    def _valid_digest(value, name):
        if not isinstance(value, str) or not _DIGEST.fullmatch(value):
            raise DomainError('validation', f'{name} must be a SHA-256 hex digest', 422)

    def _load(self, connection, project_id, operation_id, *, lock=True):
        _identifier(operation_id, 'operation_id')
        suffix = ' FOR UPDATE OF d, e' if lock else ''
        row = connection.execute(
            'SELECT d.*, e.input_digest, e.state AS effect_state, e.exposure_held '
            'FROM cpu_worker_dispatches d JOIN task_effects e USING (operation_id) '
            'WHERE d.operation_id = %s AND d.project_id = %s' + suffix,
            (operation_id, project_id)).fetchone()
        if not row:
            raise DomainError('not_found', 'CPU worker dispatch not found', 404)
        return row

    def _lock_dispatch(self, connection, project_id, operation_id):
        _identifier(operation_id, 'operation_id')
        identity = connection.execute(
            'SELECT action_id FROM cpu_worker_dispatches WHERE operation_id = %s AND project_id = %s',
            (operation_id, project_id)).fetchone()
        if not identity:
            raise DomainError('not_found', 'CPU worker dispatch not found', 404)
        connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))',
                           ('skybuild:cpu-action:' + identity['action_id'],))
        connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))',
                           ('skybuild:effect:' + operation_id,))
        return self._load(connection, project_id, operation_id)

    @staticmethod
    def _latest(connection, operation_id):
        return connection.execute(
            'SELECT * FROM cpu_worker_observations WHERE operation_id = %s '
            'ORDER BY sequence DESC LIMIT 1', (operation_id,)).fetchone()

    def _result(self, connection, dispatch):
        latest = self._latest(connection, dispatch['operation_id'])
        return _public({**dispatch, 'adapter_kind': 'cpu-worker-v1',
                        'physical_dispatch_authorized': False,
                        'launch_authorization_committed': dispatch['state'] != 'prepared',
                        'latest_observation': latest})

    def get(self, principal, project_id, operation_id):
        with self.store._connection() as connection:
            principal = self.store._authorize_effect_writer(connection, principal, project_id)
            self.store._require_api_authority(connection, project_id)
            return self._result(connection, self._load(connection, project_id, operation_id, lock=False))

    def _current_reservation(self, connection, reservation):
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
        readiness = connection.execute(
            'SELECT * FROM task_readiness WHERE project_id = %s AND task_id = %s',
            (project, task_id)).fetchone()
        if (not readiness or readiness['input_generation'] != reservation['readiness_generation']
                or readiness['assessed_generation'] != reservation['readiness_generation']):
            raise DomainError('workflow_conflict', 'CPU dispatch requires current readiness', 409)
        self.store._require_current_dependencies(connection, project, task)
        actor = self.store._principal(connection, reservation['actor'])
        self.store._authorize(connection, actor, project, 'tasks:claim')
        self.store._require_claim_fence(connection, actor, project, task_id, reservation['claim_fence'])
        claim = connection.execute(
            'SELECT * FROM task_claims WHERE project_id = %s AND task_id = %s FOR UPDATE',
            (project, task_id)).fetchone()
        from .workflow import Place
        if (not claim or not self.store._petri(task)
                or not self.store._cpu_task_binding(task, claim, reservation['readiness_generation'],
                                                    reservation['attempt_id'])
                or self.store.workflow_token(task).place != Place.WORKING
                or claim.get('task_revision') != reservation.get('claim_task_revision')):
            raise DomainError('claim_conflict', 'CPU dispatch requires the exact current Petri Working attempt', 409)
        return task, claim

    @staticmethod
    def _still_live(connection, reservation):
        return connection.execute(
            'SELECT 1 FROM task_claims WHERE project_id = %s AND task_id = %s '
            'AND held AND holder = %s AND fence = %s AND task_revision = %s '
            'AND lease_until > clock_timestamp()',
            (reservation['project_id'], reservation['task_id'], reservation['actor'],
             reservation['claim_fence'], reservation['claim_task_revision'])).fetchone()

    def prepare(self, principal, project_id, action_id, operation_id, *, profile_id, host_id,
                worker_id, unit_name, launch_nonce, source_digest, controller_head,
                controller_source_digest, controller_profile_digest, interpreter_digest, permit_digest,
                assignment_digest, patch_digest, argv_digest, approved_until):
        """Commit one immutable, pinned worker intent before local unit I/O."""
        for name, value in [('action_id', action_id), ('operation_id', operation_id), ('worker_id', worker_id)]:
            _identifier(value, name)
        if profile_id != PROFILE:
            raise DomainError('validation', 'CPU profile is not allowlisted', 422)
        _text(host_id, 'host_id', 255)
        if not _UNIT.fullmatch(unit_name or '') or not re.fullmatch(r'[0-9a-f]{32}', launch_nonce or ''):
            raise DomainError('validation', 'CPU unit or launch nonce is invalid', 422)
        for name, value in [('source_digest', source_digest), ('permit_digest', permit_digest),
                            ('controller_source_digest', controller_source_digest),
                            ('controller_profile_digest', controller_profile_digest),
                            ('interpreter_digest', interpreter_digest),
                            ('assignment_digest', assignment_digest), ('patch_digest', patch_digest),
                            ('argv_digest', argv_digest)]:
            self._valid_digest(value, name)
        if not isinstance(controller_head, str) or not re.fullmatch(r'[0-9a-f]{40}', controller_head):
            raise DomainError('validation', 'controller_head must be a Git SHA-1', 422)
        from datetime import datetime, timezone
        if not isinstance(approved_until, datetime) or approved_until.tzinfo is None:
            raise DomainError('validation', 'approved_until must have an offset', 422)
        if approved_until <= datetime.now(timezone.utc):
            raise DomainError('control_conflict', 'CPU approval interval has expired', 409)
        pins = dict(profile_id=profile_id, host_id=host_id, worker_id=worker_id, unit_name=unit_name,
                    launch_nonce=launch_nonce, source_digest=source_digest, permit_digest=permit_digest,
                    controller_head=controller_head, controller_source_digest=controller_source_digest,
                    controller_profile_digest=controller_profile_digest, interpreter_digest=interpreter_digest,
                    assignment_digest=assignment_digest, patch_digest=patch_digest, argv_digest=argv_digest,
                    approved_until=approved_until.isoformat())
        input_digest = hashlib.sha256(_json(pins).encode()).hexdigest()
        with self.store._connection() as connection:
            principal = self._lock(connection, principal, project_id)
            connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))',
                               ('skybuild:cpu-action:' + action_id,))
            connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))',
                               ('skybuild:effect:' + operation_id,))
            reservation = connection.execute(
                'SELECT * FROM cpu_reservations WHERE action_id = %s AND project_id = %s FOR UPDATE',
                (action_id, project_id)).fetchone()
            if not reservation:
                raise DomainError('not_found', 'CPU reservation not found', 404)
            prior = connection.execute(
                'SELECT d.*, e.input_digest FROM cpu_worker_dispatches d JOIN task_effects e USING (operation_id) '
                'WHERE d.action_id = %s', (action_id,)).fetchone()
            if prior:
                if prior['operation_id'] != operation_id or prior['input_digest'] != input_digest:
                    raise DomainError('idempotency_conflict', 'CPU action already binds a different worker intent', 409)
                return self._result(connection, self._load(connection, project_id, operation_id))
            if connection.execute('SELECT 1 FROM task_effects WHERE operation_id = %s', (operation_id,)).fetchone():
                raise DomainError('idempotency_conflict', 'Operation already belongs to another effect', 409)
            task, claim = self._current_reservation(connection, reservation)
            if connection.execute('SELECT 1 FROM task_effects WHERE project_id = %s AND task_id = %s '
                                  'AND exposure_held', (project_id, reservation['task_id'])).fetchone():
                raise DomainError('effect_conflict', 'Task already has unresolved effect exposure', 409)
            intent_hash = hashlib.sha256(_json(dict(adapter_kind='cpu-worker-v1', operation_id=operation_id,
                                                    action_id=action_id, reservation_hash=reservation['intent_hash'],
                                                    input_digest=input_digest, policy_digest=POLICY_DIGEST)).encode()).hexdigest()
            connection.execute(
                'INSERT INTO task_effects (operation_id, project_id, task_id, attempt_id, task_revision, '
                'authority_epoch, authority_generation, input_digest, policy_digest, allocation_refs, '
                'intent_hash, adapter_kind, state) VALUES (%s, %s, %s, %s, %s, 1, %s, %s, %s, %s, %s, %s, %s)',
                (operation_id, project_id, reservation['task_id'], reservation['attempt_id'],
                 reservation['task_revision'], reservation['generation'], input_digest, POLICY_DIGEST,
                 [action_id], intent_hash, 'cpu-worker-v1', 'unknown'))
            connection.execute(
                'INSERT INTO cpu_worker_dispatches (operation_id, action_id, reservation_hash, profile_id, '
                'project_id, task_id, attempt_id, task_revision, claim_fence, claim_task_revision, '
                'readiness_generation, pool_generation, local_generation, host_id, worker_id, unit_name, '
                'launch_nonce, source_digest, controller_head, controller_source_digest, '
                'controller_profile_digest, interpreter_digest, permit_digest, assignment_digest, patch_digest, '
                'argv_digest, approved_until) '
                'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, '
                '%s, %s, %s, %s, %s, %s, %s, %s)',
                (operation_id, action_id, reservation['intent_hash'], profile_id, project_id,
                 reservation['task_id'], reservation['attempt_id'], reservation['task_revision'],
                 reservation['claim_fence'], reservation['claim_task_revision'], reservation['readiness_generation'],
                 reservation['generation'], reservation['local_generation'], host_id, worker_id, unit_name,
                 launch_nonce, source_digest, controller_head, controller_source_digest,
                 controller_profile_digest, interpreter_digest, permit_digest, assignment_digest, patch_digest, argv_digest,
                 approved_until))
            if not self._still_live(connection, reservation):
                raise DomainError('claim_conflict', 'Claim expired before worker intent publication', 409)
            effect = _public(connection.execute('SELECT * FROM task_effects WHERE operation_id = %s',
                                                (operation_id,)).fetchone())
            self.store._effect_journal(connection, principal, effect, None,
                                       'Trusted CPU worker intent committed; local start remains one-shot')
            self.store._cpu_event(connection, principal, project_id, 'worker-intent',
                                  'Held reservation bound to one trusted CPU worker invocation', None,
                                  self._load(connection, project_id, operation_id))
            return self._result(connection, self._load(connection, project_id, operation_id))

    def begin(self, principal, project_id, operation_id):
        """Commit the one-shot launch boundary. A replay never grants another start."""
        with self.store._connection() as connection:
            principal = self._lock(connection, principal, project_id)
            dispatch = self._lock_dispatch(connection, project_id, operation_id)
            if dispatch['state'] != 'prepared':
                return {**self._result(connection, dispatch), 'start_once': False}
            reservation = connection.execute('SELECT * FROM cpu_reservations WHERE action_id = %s FOR UPDATE',
                                             (dispatch['action_id'],)).fetchone()
            self._current_reservation(connection, reservation)
            if not self._still_live(connection, reservation):
                raise DomainError('claim_conflict', 'Claim expired before worker launch authorization', 409)
            if connection.execute('SELECT %s > clock_timestamp()', (dispatch['approved_until'],)).fetchone()[0] is not True:
                raise DomainError('control_conflict', 'CPU approval interval expired before worker launch', 409)
            connection.execute("UPDATE cpu_worker_dispatches SET state = 'launch-intent' WHERE operation_id = %s",
                               (operation_id,))
            after = self._load(connection, project_id, operation_id)
            self.store._cpu_event(connection, principal, project_id, 'worker-launch-intent',
                                  'One-shot launch authorization committed before local systemd I/O', dispatch, after)
            return {**self._result(connection, after), 'start_once': True}

    def record_invocation(self, principal, project_id, operation_id, *, host_id, unit_name,
                          launch_nonce, invocation_id):
        if not _INVOCATION.fullmatch(invocation_id or '') or invocation_id == '0' * 32:
            raise DomainError('validation', 'InvocationID is invalid', 422)
        with self.store._connection() as connection:
            principal = self._lock(connection, principal, project_id)
            dispatch = self._lock_dispatch(connection, project_id, operation_id)
            self._check_identity(dispatch, host_id, unit_name, launch_nonce)
            if dispatch['invocation_id']:
                if dispatch['invocation_id'] != invocation_id:
                    raise DomainError('effect_conflict', 'Worker invocation identity changed', 409)
                return self._result(connection, dispatch)
            if dispatch['state'] not in {'launch-intent', 'unknown'}:
                raise DomainError('effect_conflict', 'Worker start intent is not committed', 409)
            connection.execute("UPDATE cpu_worker_dispatches SET state = 'running', invocation_id = %s "
                               'WHERE operation_id = %s', (invocation_id, operation_id))
            after = self._load(connection, project_id, operation_id)
            self.store._cpu_event(connection, principal, project_id, 'worker-invocation',
                                  'Systemd invocation identity pinned; reservation remains held', dispatch, after)
            return self._result(connection, after)

    @staticmethod
    def _check_identity(dispatch, host_id, unit_name, launch_nonce):
        if (dispatch['host_id'] != host_id or dispatch['unit_name'] != unit_name
                or dispatch['launch_nonce'] != launch_nonce):
            raise DomainError('effect_conflict', 'Worker host, unit or nonce differs from pinned intent', 409)

    def observe(self, principal, project_id, operation_id, *, observation_id, host_id, unit_name,
                launch_nonce, invocation_id, phase, result=None, exit_status=None,
                worker_result_digest=None):
        try:
            observation_id = UUID(str(observation_id))
        except (ValueError, TypeError, AttributeError):
            raise DomainError('validation', 'observation_id must be a UUID', 422) from None
        if phase not in {'running', 'unknown', 'failed', 'completed'}:
            raise DomainError('validation', 'Observation phase is invalid', 422)
        if phase == 'completed':
            if result != 'success' or type(exit_status) is not int or exit_status != 0:
                raise DomainError('validation', 'Only successful natural completion is settleable', 422)
            self._valid_digest(worker_result_digest, 'worker_result_digest')
        elif phase == 'failed':
            if not isinstance(result, str) or not result or len(result) > 100 or worker_result_digest is not None:
                raise DomainError('validation', 'Failed terminal evidence is invalid', 422)
        elif result is not None or exit_status is not None or worker_result_digest is not None:
            raise DomainError('validation', 'Nonterminal observations cannot carry result fields', 422)
        with self.store._connection() as connection:
            principal = self._lock(connection, principal, project_id)
            dispatch = self._lock_dispatch(connection, project_id, operation_id)
            self._check_identity(dispatch, host_id, unit_name, launch_nonce)
            if dispatch['invocation_id'] != invocation_id:
                raise DomainError('effect_conflict', 'Observation InvocationID differs from pinned unit', 409)
            prior = connection.execute('SELECT * FROM cpu_worker_observations WHERE observation_id = %s',
                                       (observation_id,)).fetchone()
            values = (operation_id, dispatch['action_id'], host_id, unit_name, launch_nonce,
                      invocation_id, phase, result, exit_status, worker_result_digest)
            if prior:
                expected = (prior['operation_id'], prior['action_id'], prior['host_id'], prior['unit_name'],
                            prior['launch_nonce'], prior['invocation_id'], prior['phase'], prior['result'],
                            prior['exit_status'], prior['worker_result_digest'])
                if expected != values:
                    raise DomainError('idempotency_conflict', 'Observation ID was replayed with different evidence', 409)
                return self._result(connection, dispatch)
            if dispatch['state'] not in {'running', 'unknown'}:
                raise DomainError('effect_conflict', 'Worker dispatch cannot accept another observation', 409)
            latest = self._latest(connection, operation_id)
            if latest and latest['phase'] in {'completed', 'failed'}:
                raise DomainError('effect_conflict', 'A terminal unit observation cannot be overwritten', 409)
            sequence = 1 if not latest else latest['sequence'] + 1
            connection.execute(
                'INSERT INTO cpu_worker_observations (observation_id, operation_id, action_id, host_id, unit_name, '
                'launch_nonce, invocation_id, sequence, phase, result, exit_status, worker_result_digest) '
                'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)',
                (observation_id, operation_id, dispatch['action_id'], host_id, unit_name, launch_nonce,
                 invocation_id, sequence, phase, result, exit_status, worker_result_digest))
            if phase == 'completed':
                connection.execute("UPDATE cpu_worker_dispatches SET state = 'terminal' WHERE operation_id = %s",
                                   (operation_id,))
            elif phase in {'unknown', 'failed'}:
                connection.execute("UPDATE cpu_worker_dispatches SET state = 'unknown' WHERE operation_id = %s",
                                   (operation_id,))
            else:
                connection.execute("UPDATE cpu_worker_dispatches SET state = 'running' WHERE operation_id = %s",
                                   (operation_id,))
            after = self._load(connection, project_id, operation_id)
            self.store._cpu_event(connection, principal, project_id, 'worker-observation',
                                  'Exact unit observation persisted; no task completion accepted', dispatch, after)
            return self._result(connection, after)

    def settle(self, principal, project_id, operation_id, *, observation_id):
        try:
            observation_id = UUID(str(observation_id))
        except (ValueError, TypeError, AttributeError):
            raise DomainError('validation', 'observation_id must be a UUID', 422) from None
        with self.store._connection() as connection:
            principal = self._lock(connection, principal, project_id)
            dispatch = self._lock_dispatch(connection, project_id, operation_id)
            if dispatch['state'] == 'settled':
                return self._result(connection, dispatch)
            if dispatch['state'] != 'terminal':
                raise DomainError('effect_conflict', 'Natural terminal observation is required for settlement', 409)
            proof = connection.execute(
                'SELECT * FROM cpu_worker_observations WHERE observation_id = %s AND operation_id = %s',
                (observation_id, operation_id)).fetchone()
            if (not proof or proof['phase'] != 'completed' or proof['result'] != 'success'
                    or proof['exit_status'] != 0 or proof['worker_result_digest'] is None
                    or (proof['action_id'], proof['host_id'], proof['unit_name'], proof['launch_nonce'],
                        proof['invocation_id']) != (dispatch['action_id'], dispatch['host_id'],
                        dispatch['unit_name'], dispatch['launch_nonce'], dispatch['invocation_id'])):
                raise DomainError('effect_conflict', 'Terminal observation does not prove the pinned worker result', 409)
            reservation = connection.execute(
                'SELECT * FROM cpu_reservations WHERE action_id = %s AND project_id = %s FOR UPDATE',
                (dispatch['action_id'], project_id)).fetchone()
            if (not reservation or reservation['state'] != 'reserved'
                    or reservation['intent_hash'] != dispatch['reservation_hash']
                    or reservation['attempt_id'] != dispatch['attempt_id']
                    or reservation['claim_fence'] != dispatch['claim_fence']):
                raise DomainError('capacity_conflict', 'Pinned CPU reservation changed before settlement', 409)
            effect_before = _public(connection.execute('SELECT * FROM task_effects WHERE operation_id = %s FOR UPDATE',
                                                        (operation_id,)).fetchone())
            if effect_before['state'] != 'unknown' or not effect_before['exposure_held']:
                raise DomainError('effect_conflict', 'Worker effect is not held for settlement', 409)
            connection.execute("UPDATE cpu_worker_dispatches SET state = 'settled' WHERE operation_id = %s",
                               (operation_id,))
            connection.execute("UPDATE task_effects SET state = 'settled', exposure_held = false WHERE operation_id = %s",
                               (operation_id,))
            connection.execute("UPDATE cpu_reservations SET state = 'released' WHERE action_id = %s",
                               (dispatch['action_id'],))
            effect_after = _public(connection.execute('SELECT * FROM task_effects WHERE operation_id = %s',
                                                       (operation_id,)).fetchone())
            self.store._effect_journal(connection, principal, effect_after, effect_before,
                                       'Exact natural unit completion and worker result verified; no task completion accepted')
            after = self._load(connection, project_id, operation_id)
            self.store._cpu_event(connection, principal, project_id, 'worker-settle',
                                  'Exact natural terminal observation releases its existing reservation', dispatch, after)
            return self._result(connection, after)
