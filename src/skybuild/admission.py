"""Unredeemable, project-scoped CPU reservations; never execution authority."""
import hashlib
from uuid import uuid4

from psycopg.types.json import Jsonb

from .contracts import DomainError


def _integer(value, name, *, zero=False, maximum=2**63 - 1):
    from .store import _invalid
    if type(value) is not int or not (0 if zero else 1) <= value <= maximum:
        _invalid(f'{name} must be an integer in range')


class CPUAdmission:
    @staticmethod
    def _cpu_lock(connection, project_id):
        connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))',
                           ('skybuild:cpu:' + project_id,))

    @staticmethod
    def _cpu_event(connection, principal, project_id, action, reason, before, after):
        from .store import _public
        connection.execute('INSERT INTO cpu_journal (event_id, project_id, actor, action, reason, before_state, after_state) '
                           'VALUES (%s, %s, %s, %s, %s, %s, %s)',
                           (uuid4(), project_id, principal.principal_id, action, reason,
                            Jsonb(_public(before)) if before else None, Jsonb(_public(after))))

    def configure_cpu_pool(self, principal, project_id, capacity, enabled, expected_generation, idempotency_key, *, reason):
        """Owner CAS of central restriction. New pools start locally disabled."""
        _integer(capacity, 'capacity', zero=True, maximum=2**31 - 1)
        return self._cpu_control(principal, project_id, enabled, expected_generation, idempotency_key,
                                 reason=reason, capacity=capacity)

    def set_cpu_local_control(self, principal, project_id, enabled, expected_generation, idempotency_key, *, reason):
        """Owner CAS of independent local restriction; no central override."""
        return self._cpu_control(principal, project_id, enabled, expected_generation, idempotency_key, reason=reason)

    def _cpu_control(self, principal, project_id, enabled, expected_generation, key, *, reason, capacity=None):
        from .store import _invalid, _public, _text
        if type(enabled) is not bool:
            _invalid('enabled must be boolean')
        _integer(expected_generation, 'expected_generation', zero=True)
        _text(reason, 'reason', 4096)
        local = capacity is None
        payload = dict(enabled=enabled, generation=expected_generation, capacity=capacity, reason=reason)
        with self._connection() as connection:
            principal = self._authorize(connection, principal, project_id, 'tasks:write')
            if not principal.is_admin:
                raise DomainError('authorization', 'Only owner/admin may configure CPU controls', 403)
            self._require_api_authority(connection, project_id)
            def mutation():
                self._cpu_lock(connection, project_id)
                before = connection.execute('SELECT * FROM cpu_pools WHERE project_id = %s FOR UPDATE', (project_id,)).fetchone()
                field = 'local_generation' if local else 'generation'
                if (before[field] if before else 0) != expected_generation or (local and not before):
                    raise DomainError('stale_revision', 'CPU control generation has changed', 409)
                if expected_generation == 2**63 - 1:
                    raise DomainError('control_conflict', 'CPU control generation exhausted', 409)
                if not local:
                    held = connection.execute("SELECT COALESCE(sum(units), 0) AS units FROM cpu_reservations WHERE project_id = %s AND state = 'reserved'", (project_id,)).fetchone()['units']
                    if capacity < held:
                        raise DomainError('capacity_conflict', 'Capacity cannot fall below held reservations', 409)
                if not before:
                    connection.execute('INSERT INTO cpu_pools (project_id, capacity, enabled, generation) VALUES (%s, %s, %s, 1)', (project_id, capacity, enabled))
                elif local:
                    connection.execute('UPDATE cpu_pools SET local_enabled = %s, local_generation = local_generation + 1 WHERE project_id = %s', (enabled, project_id))
                else:
                    connection.execute('UPDATE cpu_pools SET capacity = %s, enabled = %s, generation = generation + 1 WHERE project_id = %s', (capacity, enabled, project_id))
                after = connection.execute('SELECT * FROM cpu_pools WHERE project_id = %s', (project_id,)).fetchone()
                self._cpu_event(connection, principal, project_id, 'local-control' if local else 'central-control', reason, before, after)
                return _public(after)
            return self._idempotent(connection, principal, project_id, 'cpu.local' if local else 'cpu.central', key, payload, mutation)

    def reserve_cpu(self, principal, project_id, task_id, action_id, attempt_id, units, expected_revision,
                    readiness_generation, claim_fence, generation, local_generation):
        """Reserve units, never a permit. Replay returns current cancellation state.

        Lock order: project graph, project CPU pool, global action, task, claim.
        Stop controls acquire only the pool lock and cannot race publication.
        """
        from .store import _identifier, _json, _public
        for name, value in [('task_id', task_id), ('action_id', action_id), ('attempt_id', attempt_id)]:
            _identifier(value, name)
        for name, value in [('units', units), ('expected_revision', expected_revision),
                            ('readiness_generation', readiness_generation), ('claim_fence', claim_fence),
                            ('generation', generation), ('local_generation', local_generation)]:
            _integer(value, name, maximum=2**31 - 1 if name == 'units' else 2**63 - 1)
        with self._connection() as connection:
            principal = self._authorize(connection, principal, project_id, 'tasks:claim')
            self._require_api_authority(connection, project_id)
            payload = dict(project_id=project_id, task_id=task_id, actor=principal.principal_id,
                           action_id=action_id, attempt_id=attempt_id, units=units, task_revision=expected_revision,
                           readiness_generation=readiness_generation, claim_fence=claim_fence,
                           generation=generation, local_generation=local_generation)
            digest = hashlib.sha256(_json(payload).encode()).hexdigest()
            self._graph_lock(connection, project_id)
            self._cpu_lock(connection, project_id)
            connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))', ('skybuild:cpu-action:' + action_id,))
            connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))', ('skybuild:cpu-attempt:' + attempt_id,))
            prior = connection.execute('SELECT * FROM cpu_reservations WHERE action_id = %s', (action_id,)).fetchone()
            if prior:
                if prior['intent_hash'] != digest:
                    raise DomainError('idempotency_conflict', 'CPU action identity has different intent', 409)
                return _public(prior)
            pool = connection.execute('SELECT * FROM cpu_pools WHERE project_id = %s FOR UPDATE', (project_id,)).fetchone()
            if not pool or not pool['enabled'] or not pool['local_enabled'] or pool['generation'] != generation or pool['local_generation'] != local_generation:
                raise DomainError('control_conflict', 'CPU controls are absent, disabled or stale', 409)
            task = self._task(connection, project_id, task_id, lock=True)
            if task['revision'] != expected_revision:
                raise DomainError('stale_revision', 'Task revision has changed', 409)
            claim = connection.execute('SELECT *, lease_until > clock_timestamp() AS live FROM task_claims WHERE project_id = %s AND task_id = %s FOR UPDATE', (project_id, task_id)).fetchone()
            if not claim or not claim['held'] or not claim['live'] or claim['fence'] != claim_fence or claim['holder'] != principal.principal_id or claim['task_revision'] != expected_revision:
                raise DomainError('claim_conflict', 'CPU reservation requires current claim holder, lease and fence', 409)
            readiness = connection.execute('SELECT * FROM task_readiness WHERE project_id = %s AND task_id = %s', (project_id, task_id)).fetchone()
            if task['status'] != 'ready' or not readiness or readiness['input_generation'] != readiness_generation or readiness['assessed_generation'] != readiness_generation:
                raise DomainError('workflow_conflict', 'CPU reservation requires current readiness', 409)
            self._require_current_dependencies(connection, project_id, task)
            self._require_no_external_exposure(connection, project_id, task_id)
            if connection.execute("SELECT 1 FROM cpu_reservations WHERE project_id = %s AND task_id = %s AND state = 'reserved'", (project_id, task_id)).fetchone():
                raise DomainError('capacity_conflict', 'Task already has held CPU reservation', 409)
            held = connection.execute("SELECT COALESCE(sum(units), 0) AS units FROM cpu_reservations WHERE project_id = %s AND state = 'reserved'", (project_id,)).fetchone()['units']
            if held + units > pool['capacity']:
                raise DomainError('capacity_conflict', 'CPU pool capacity exhausted', 409)
            if connection.execute('SELECT 1 FROM cpu_reservations WHERE attempt_id = %s', (attempt_id,)).fetchone():
                raise DomainError('idempotency_conflict', 'CPU attempt identity already exists', 409)
            inserted = connection.execute('INSERT INTO cpu_reservations (action_id, attempt_id, project_id, task_id, actor, claim_fence, task_revision, readiness_generation, generation, local_generation, units, intent_hash) '
                                          'SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s '
                                          'WHERE EXISTS (SELECT 1 FROM task_claims WHERE project_id = %s AND task_id = %s '
                                          'AND held AND holder = %s AND fence = %s AND task_revision = %s AND lease_until > clock_timestamp())',
                                          (action_id, attempt_id, project_id, task_id, principal.principal_id, claim_fence, expected_revision, readiness_generation, generation, local_generation, units, digest,
                                           project_id, task_id, principal.principal_id, claim_fence, expected_revision))
            if inserted.rowcount != 1:
                raise DomainError('claim_conflict', 'Ownership lease expired before CPU reservation', 409)
            after = connection.execute('SELECT * FROM cpu_reservations WHERE action_id = %s', (action_id,)).fetchone()
            self._cpu_event(connection, principal, project_id, 'reserve', 'Unredeemable reservation; no dispatch authorized', None, after)
            return _public(after)

    def cancel_cpu_reservation(self, principal, project_id, action_id, *, reason):
        """Cancel only never-dispatched reservation. No claim reconciliation implied."""
        from .store import _identifier, _public, _text
        _identifier(action_id, 'action_id')
        _text(reason, 'reason', 4096)
        with self._connection() as connection:
            principal = self._authorize(connection, principal, project_id, 'tasks:claim')
            self._require_api_authority(connection, project_id)
            self._graph_lock(connection, project_id)
            self._cpu_lock(connection, project_id)
            reservation = connection.execute('SELECT * FROM cpu_reservations WHERE action_id = %s AND project_id = %s FOR UPDATE', (action_id, project_id)).fetchone()
            if not reservation:
                raise DomainError('not_found', 'CPU reservation not found', 404)
            if not principal.is_admin and reservation['actor'] != principal.principal_id:
                raise DomainError('authorization', 'Only reservation actor or owner/admin may cancel', 403)
            if reservation['state'] == 'cancelled':
                return _public(reservation)
            self._task(connection, project_id, reservation['task_id'], lock=True)
            if not principal.is_admin:
                self._require_claim_fence(connection, principal, project_id, reservation['task_id'], reservation['claim_fence'])
            # Any effect history may represent dispatch; terminal state alone does
            # not prove that this reservation was never dispatched.
            if connection.execute('SELECT 1 FROM task_effects WHERE project_id = %s AND task_id = %s LIMIT 1', (project_id, reservation['task_id'])).fetchone():
                raise DomainError('effect_conflict', 'Effect history prevents never-dispatched cancellation', 409)
            connection.execute("UPDATE cpu_reservations SET state = 'cancelled' WHERE action_id = %s", (action_id,))
            after = connection.execute('SELECT * FROM cpu_reservations WHERE action_id = %s', (action_id,)).fetchone()
            self._cpu_event(connection, principal, project_id, 'cancel', reason, reservation, after)
            return _public(after)
