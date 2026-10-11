"""Owner-attested recovery of one unstarted CPU launch intent.

The owner must first stop and fence the exact launcher on the execution host.
Absence alone is insufficient. This route records that explicit attestation;
it cannot inspect a remote host and does not claim successful worker execution.
"""

from datetime import datetime, timedelta, timezone
import hashlib
from uuid import UUID

from psycopg.types.json import Jsonb

from .contracts import DomainError
from .cpu_worker_dispatch import CPUWorkerDispatch, _INVOCATION
from .store import _json, _public, _text


PROOF_FIELDS = {
    'host_id', 'unit_name', 'launch_nonce', 'controller_unit', 'controller_invocation_id',
    'launcher_stopped', 'launcher_cgroup_empty', 'launcher_fenced', 'unit_absent',
    'container_absent', 'manifest_absent', 'fence_sha256', 'evidence_sha256', 'observed_at',
}
FACTS = {'launcher_stopped', 'launcher_cgroup_empty', 'launcher_fenced',
         'unit_absent', 'container_absent', 'manifest_absent'}


def validate_proof(proof):
    """Reject incomplete or contradictory owner statements before any mutation."""
    if not isinstance(proof, dict) or set(proof) != PROOF_FIELDS:
        raise DomainError('validation', 'Recovery proof fields differ from the exact contract', 422)
    if any(proof[name] is not True for name in FACTS):
        raise DomainError('validation', 'Recovery requires a stopped, fenced launcher and absent worker', 422)
    for name in ('fence_sha256', 'evidence_sha256'):
        CPUWorkerDispatch._valid_digest(proof[name], name)
    for name in ('host_id', 'unit_name', 'launch_nonce', 'controller_unit'):
        _text(proof[name], name, 255)
    invocation = proof['controller_invocation_id']
    if not isinstance(invocation, str) or not _INVOCATION.fullmatch(invocation) or invocation == '0' * 32:
        raise DomainError('validation', 'Recovery needs the exact stopped controller InvocationID', 422)
    try:
        observed = datetime.fromisoformat(proof['observed_at'])
        if observed.tzinfo is None:
            raise ValueError
    except (ValueError, TypeError):
        raise DomainError('validation', 'Recovery observation requires an offset timestamp', 422) from None
    now = datetime.now(timezone.utc)
    if not now - timedelta(minutes=10) <= observed <= now:
        raise DomainError('effect_conflict', 'Recovery host proof is stale or in the future', 409)
    return observed


class CPUWorkerRecovery:
    def __init__(self, store):
        self.store = store
        self.dispatches = CPUWorkerDispatch(store)

    def get(self, principal, project_id, operation_id):
        with self.store._connection() as connection:
            self.store._authorize_effect_writer(connection, principal, project_id)
            self.store._require_api_authority(connection, project_id)
            dispatch = self.dispatches._load(connection, project_id, operation_id, lock=False)
            receipt = connection.execute(
                'SELECT * FROM cpu_worker_recoveries WHERE operation_id = %s', (operation_id,)).fetchone()
            if not receipt:
                raise DomainError('not_found', 'CPU worker recovery not found', 404)
            return self._result(connection, dispatch, receipt)

    def recover(self, principal, project_id, operation_id, *, recovery_id, expected_revision,
                claim_fence, attempt_id, reservation_hash, proof):
        try:
            recovery_id = UUID(str(recovery_id))
        except (TypeError, ValueError, AttributeError):
            raise DomainError('validation', 'recovery_id must be a UUID', 422) from None
        for name, value in [('expected_revision', expected_revision), ('claim_fence', claim_fence)]:
            if type(value) is not int or not 0 < value < 2**63:
                raise DomainError('validation', name + ' must be a positive integer', 422)
        _text(attempt_id, 'attempt_id', 200)
        self.dispatches._valid_digest(reservation_hash, 'reservation_hash')
        # Validate structure now; freshness is required only for a new operation.
        # A committed exact replay remains readable after the proof ages.
        payload = dict(expected_revision=expected_revision, claim_fence=claim_fence,
                       attempt_id=attempt_id, reservation_hash=reservation_hash, proof=proof)
        digest = hashlib.sha256(_json(payload).encode()).hexdigest()
        with self.store._connection() as connection:
            principal = self.dispatches._lock(connection, principal, project_id)
            connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))',
                               ('skybuild:cpu-recovery:' + str(recovery_id),))
            dispatch = self.dispatches._lock_dispatch(connection, project_id, operation_id)
            prior = connection.execute(
                'SELECT * FROM cpu_worker_recoveries WHERE recovery_id = %s OR operation_id = %s',
                (recovery_id, operation_id)).fetchall()
            if prior:
                if (len(prior) != 1 or prior[0]['recovery_id'] != recovery_id
                        or prior[0]['operation_id'] != operation_id
                        or prior[0]['actor'] != principal.principal_id
                        or prior[0]['request_digest'] != digest):
                    raise DomainError('idempotency_conflict', 'Recovery identity or evidence changed', 409)
                return self._result(connection, dispatch, prior[0])
            observed = validate_proof(proof)
            self.dispatches._check_identity(dispatch, proof['host_id'], proof['unit_name'], proof['launch_nonce'])
            if observed < dispatch['created_at']:
                raise DomainError('effect_conflict', 'Recovery proof predates this dispatch', 409)
            task = self.store._task(connection, project_id, dispatch['task_id'], lock=True)
            token = self.store.workflow_token(task)
            claim = connection.execute(
                'SELECT * FROM task_claims WHERE project_id = %s AND task_id = %s FOR UPDATE',
                (project_id, dispatch['task_id'])).fetchone()
            # Expiry does not clear held exposure. The owner may correct the exact
            # retained attempt, but cannot correct a newer claim or task revision.
            if (task['revision'] != expected_revision or token.place.value != 'working'
                    or token.attempt_id != attempt_id or token.claim_fence != claim_fence
                    or dispatch['attempt_id'] != attempt_id or dispatch['claim_fence'] != claim_fence
                    or not claim or claim['fence'] != claim_fence
                    or claim['holder'] is None or claim['task_revision'] != dispatch['claim_task_revision']):
                raise DomainError('claim_conflict', 'Recovery requires the exact current task revision and attempt fence', 409)
            reservation = connection.execute(
                'SELECT * FROM cpu_reservations WHERE action_id = %s AND project_id = %s FOR UPDATE',
                (dispatch['action_id'], project_id)).fetchone()
            if (not reservation or reservation['state'] != 'reserved'
                    or reservation['intent_hash'] != reservation_hash
                    or dispatch['reservation_hash'] != reservation_hash
                    or reservation['attempt_id'] != attempt_id or reservation['claim_fence'] != claim_fence
                    or reservation['actor'] != claim['holder']):
                raise DomainError('capacity_conflict', 'Recovery reservation identity changed', 409)
            if (dispatch['state'] != 'launch-intent' or dispatch['invocation_id'] is not None
                    or self.dispatches._latest(connection, operation_id) is not None
                    or dispatch['effect_state'] != 'unknown' or not dispatch['exposure_held']):
                raise DomainError('effect_conflict', 'Recovery requires an unstarted held launch intent', 409)
            effect_before = _public(connection.execute(
                'SELECT * FROM task_effects WHERE operation_id = %s FOR UPDATE', (operation_id,)).fetchone())
            receipt = connection.execute(
                'INSERT INTO cpu_worker_recoveries '
                '(recovery_id, operation_id, action_id, actor, request_digest, evidence) '
                'VALUES (%s, %s, %s, %s, %s, %s) RETURNING *',
                (recovery_id, operation_id, dispatch['action_id'], principal.principal_id,
                 digest, Jsonb(payload))).fetchone()
            connection.execute("UPDATE cpu_worker_dispatches SET state = 'cancelled' WHERE operation_id = %s",
                               (operation_id,))
            connection.execute("UPDATE task_effects SET state = 'cancelled', exposure_held = false WHERE operation_id = %s",
                               (operation_id,))
            connection.execute("UPDATE cpu_reservations SET state = 'cancelled' WHERE action_id = %s",
                               (dispatch['action_id'],))
            effect_after = _public(connection.execute(
                'SELECT * FROM task_effects WHERE operation_id = %s', (operation_id,)).fetchone())
            self.store._effect_journal(connection, principal, effect_after, effect_before,
                'Owner attests exact launcher stopped and fenced; worker never started; no success accepted')
            after = self.dispatches._load(connection, project_id, operation_id)
            self.store._cpu_event(connection, principal, project_id, 'worker-unstarted-recovery',
                'Owner recovery cancels the exact held reservation once; task and claim history are preserved',
                dispatch, after)
            return self._result(connection, after, receipt)

    def _result(self, connection, dispatch, receipt):
        return {**self.dispatches._result(connection, dispatch), 'recovery': _public(receipt),
                'worker_success_accepted': False}
