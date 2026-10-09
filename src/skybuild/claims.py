"""Launch-free ownership. Expiry fences writes but never frees exposure."""
from uuid import uuid4

from psycopg.types.json import Jsonb

from .contracts import DomainError


class Claims:
    def claim_task(self, principal, project_id, task_id, expected_revision, idempotency_key, *, lease_seconds=60):
        return self._claim_transition(principal, project_id, task_id, 'claim', expected_revision,
                                      idempotency_key, lease_seconds=lease_seconds)

    def renew_claim(self, principal, project_id, task_id, fence, expected_revision, idempotency_key, *, lease_seconds=60):
        return self._claim_transition(principal, project_id, task_id, 'renew', expected_revision,
                                      idempotency_key, fence=fence, lease_seconds=lease_seconds)

    def release_claim(self, principal, project_id, task_id, fence, expected_revision, idempotency_key, *, reason):
        return self._claim_transition(principal, project_id, task_id, 'release', expected_revision,
                                      idempotency_key, fence=fence, reason=reason)

    def reconcile_claim(self, principal, project_id, task_id, fence, expected_revision, idempotency_key, *, reason):
        """Owner attests quiescence; unresolved effects still block release."""
        return self._claim_transition(principal, project_id, task_id, 'reconcile', expected_revision,
                                      idempotency_key, fence=fence, reason=reason)

    def _claim_transition(self, principal, project_id, task_id, action, expected_revision, key,
                          *, fence=None, lease_seconds=60, reason='Ownership only; no execution authorized'):
        from .store import _identifier, _invalid, _public, _text
        _identifier(task_id, 'task_id')
        _text(reason, 'reason', 4096)
        if type(expected_revision) is not int or not 1 <= expected_revision < 2**63:
            _invalid('Claims require a positive expected task revision')
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 300:
            _invalid('Lease seconds must be an integer from 1 to 300')
        if action != 'claim' and (type(fence) is not int or not 1 <= fence < 2**63):
            _invalid('Claims require a positive fence')
        payload = dict(task_id=task_id, revision=expected_revision, fence=fence,
                       lease_seconds=lease_seconds, reason=reason)
        with self._connection() as connection:
            principal = self._authorize(connection, principal, project_id, 'tasks:claim')
            self._require_api_authority(connection, project_id)
            if action == 'reconcile' and not principal.is_admin:
                raise DomainError('authorization', 'Only an owner/admin may reconcile ownership', 403)

            def mutation():
                self._graph_lock(connection, project_id)
                task = self._task(connection, project_id, task_id, lock=True)
                if task['revision'] != expected_revision:
                    raise DomainError('stale_revision', 'Task revision has changed', 409)
                before = connection.execute('SELECT *, lease_until > clock_timestamp() AS live FROM task_claims '
                                            'WHERE project_id = %s AND task_id = %s FOR UPDATE',
                                            (project_id, task_id)).fetchone()
                if action == 'claim':
                    if before and before['held']:
                        raise DomainError('claim_conflict', 'Ownership remains held, including after lease expiry', 409)
                    if task['status'] != 'ready':
                        raise DomainError('workflow_conflict', 'Only ready tasks may be claimed', 409)
                    self._require_current_dependencies(connection, project_id, task)
                    self._require_no_effect_exposure(connection, project_id, task_id)
                    if before and before['fence'] == 2**63 - 1:
                        raise DomainError('claim_conflict', 'Ownership fence exhausted', 409)
                    connection.execute('INSERT INTO task_claims (project_id, task_id, fence, holder, task_revision, lease_until) '
                                       "VALUES (%s, %s, 1, %s, %s, clock_timestamp() + %s * interval '1 second') "
                                       'ON CONFLICT (project_id, task_id) DO UPDATE SET fence = task_claims.fence + 1, '
                                       'holder = EXCLUDED.holder, task_revision = EXCLUDED.task_revision, '
                                       'lease_until = EXCLUDED.lease_until, held = true',
                                       (project_id, task_id, principal.principal_id, expected_revision, lease_seconds))
                else:
                    if not before or not before['held'] or before['fence'] != fence:
                        raise DomainError('claim_conflict', 'Ownership fence is not current', 409)
                    if action != 'reconcile' and (before['holder'] != principal.principal_id or not before['live']):
                        raise DomainError('claim_conflict', 'Ownership holder or lease is not current', 409)
                    if action != 'renew':
                        self._require_no_external_exposure(connection, project_id, task_id)
                        connection.execute('UPDATE task_claims SET held = false WHERE project_id = %s AND task_id = %s',
                                           (project_id, task_id))
                    else:
                        connection.execute("UPDATE task_claims SET lease_until = clock_timestamp() + %s * interval '1 second' "
                                           'WHERE project_id = %s AND task_id = %s', (lease_seconds, project_id, task_id))
                after = _public(connection.execute('SELECT * FROM task_claims WHERE project_id = %s AND task_id = %s',
                                                   (project_id, task_id)).fetchone())
                if before:
                    before.pop('live')
                connection.execute('INSERT INTO claim_journal (event_id, project_id, task_id, actor, action, reason, '
                                   'before_state, after_state) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)',
                                   (uuid4(), project_id, task_id, principal.principal_id, action, reason,
                                    Jsonb(_public(before)) if before else None, Jsonb(after)))
                return after
            return self._idempotent(connection, principal, project_id, 'claim.' + action, key, payload, mutation)

    @staticmethod
    def _require_api_authority(connection, project_id):
        if connection.execute("SELECT 1 FROM ledger_imports WHERE project_id = %s AND authority = 'markdown' FOR SHARE",
                              (project_id,)).fetchone():
            raise DomainError('authority', 'Markdown ledger remains task authority', 409)

    @staticmethod
    def _require_no_external_exposure(connection, project_id, task_id):
        if connection.execute('SELECT 1 FROM task_effects WHERE project_id = %s AND task_id = %s '
                              'AND exposure_held LIMIT 1', (project_id, task_id)).fetchone():
            raise DomainError('effect_conflict', 'Task has unresolved effect exposure', 409)

    def claim_history(self, principal, project_id, task_id, *, limit=100, offset=0):
        from .store import _public
        self._page(limit, offset)
        with self._connection() as connection:
            self._authorize(connection, principal, project_id, 'tasks:read')
            self._task(connection, project_id, task_id)
            return _public(connection.execute('SELECT * FROM claim_journal WHERE project_id = %s AND task_id = %s '
                                              'ORDER BY created_at, event_id LIMIT %s OFFSET %s',
                                              (project_id, task_id, limit, offset)).fetchall())
