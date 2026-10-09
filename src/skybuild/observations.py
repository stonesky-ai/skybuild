"""Launch-free observation receipt. Evidence never changes execution authority."""
from datetime import datetime, timezone
import hashlib

from psycopg.types.json import Jsonb

from .contracts import DomainError


class Observations:
    def record_observation(self, principal, project_id, body):
        """Append evidence, atomically advancing only its pinned, current identity.

        The first current identity pins each task/component/source projection.
        Reboots/PID changes remain history; rollover requires later reconciliation.
        Reported exit/result is evidence, never accepted task completion.
        """
        from .store import _body, _identifier, _invalid, _json, _public, _text
        fields = {'event_id', 'task_id', 'attempt_id', 'claim_fence', 'component_id',
                  'source_id', 'boot_id', 'pid', 'process_start', 'source_sequence',
                  'observed_at', 'state', 'evidence_refs'}
        _body(body, fields)
        if set(body) != fields:
            _invalid('Observation requires every identity and evidence field')
        for name in ('event_id', 'task_id', 'attempt_id', 'component_id', 'source_id', 'boot_id'):
            _identifier(body[name], name)
        for name in ('claim_fence', 'source_sequence', 'pid'):
            value = body[name]
            if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 2**63:
                _invalid(f'{name} must be a positive bigint')
        _text(body['process_start'], 'process_start', 200)
        if not isinstance(body['state'], str) or body['state'] not in ('running', 'waiting', 'unknown', 'exited', 'interrupted', 'result-reported'):
            _invalid('Observation state must describe evidence, not accepted completion')
        refs = body['evidence_refs']
        if not isinstance(refs, list) or len(refs) > 8:
            _invalid('evidence_refs must contain at most eight opaque artifact references')
        for ref in refs:
            _identifier(ref, 'evidence_ref')
        try:
            observed = datetime.fromisoformat(body['observed_at'])
            if observed.tzinfo is None or observed.utcoffset() is None:
                raise ValueError
            observed = observed.astimezone(timezone.utc)
        except (ValueError, TypeError, OverflowError):
            _invalid('observed_at must be an offset-aware ISO timestamp')
        identity = {key: body[key] for key in ('task_id', 'attempt_id', 'claim_fence',
                    'component_id', 'source_id', 'boot_id', 'pid', 'process_start')}
        identity_hash = hashlib.sha256(_json(identity).encode()).hexdigest()
        with self._connection() as connection:
            principal = self._authorize(connection, principal, project_id, 'tasks:claim')
            # Receipt is not a task mutation: Markdown authority is untouched.
            self._task(connection, project_id, body['task_id'])
            reservation = connection.execute('SELECT * FROM cpu_reservations WHERE project_id = %s AND attempt_id = %s FOR SHARE',
                                             (project_id, body['attempt_id'])).fetchone()
            if not reservation or reservation['task_id'] != body['task_id'] or reservation['claim_fence'] != body['claim_fence']:
                raise DomainError('observation_identity', 'Observation must reference its reserved attempt and fence', 409)
            if not principal.is_admin and reservation['actor'] != principal.principal_id:
                raise DomainError('authorization', 'Only the attempt actor or owner/admin may report evidence', 403)
            payload = dict(body, observed_at=observed.isoformat(), actor=principal.principal_id)
            digest = hashlib.sha256(_json(payload).encode()).hexdigest()
            # Serializes event-ID/sequence conflict checks and projection publication.
            connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))',
                               ('skybuild:observation:' + project_id,))
            prior = connection.execute('SELECT * FROM observation_events WHERE project_id = %s AND '
                                       '(event_id = %s OR (identity_hash = %s AND source_sequence = %s))',
                                       (project_id, body['event_id'], identity_hash, body['source_sequence'])).fetchall()
            if prior:
                if len(prior) != 1 or prior[0]['payload_hash'] != digest:
                    raise DomainError('idempotency_conflict', 'Observation identity or sequence has different evidence', 409)
                return _public(prior[0])
            # Claim locking prevents release/reacquisition racing projection eligibility.
            claim = connection.execute('SELECT * FROM task_claims WHERE project_id = %s AND task_id = %s FOR SHARE',
                                       (project_id, body['task_id'])).fetchone()
            event = connection.execute('INSERT INTO observation_events '
                '(project_id, event_id, task_id, attempt_id, actor, identity_hash, identity, source_sequence, observed_at, state, evidence_refs, payload_hash) '
                'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *',
                (project_id, body['event_id'], body['task_id'], body['attempt_id'], principal.principal_id,
                 identity_hash, Jsonb(identity), body['source_sequence'], observed, body['state'], Jsonb(refs), digest)).fetchone()
            if reservation['state'] == 'reserved' and claim and claim['held'] and claim['fence'] == body['claim_fence'] and claim['holder'] == reservation['actor']:
                # Lease expiry is deliberately not interpreted as death or release.
                connection.execute('INSERT INTO observation_projections '
                    '(project_id, task_id, component_id, source_id, identity_hash, source_sequence, event_id) '
                    'VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (project_id, task_id, component_id, source_id) '
                    'DO UPDATE SET source_sequence = EXCLUDED.source_sequence, event_id = EXCLUDED.event_id '
                    'WHERE observation_projections.identity_hash = EXCLUDED.identity_hash '
                    'AND observation_projections.source_sequence < EXCLUDED.source_sequence',
                    (project_id, body['task_id'], body['component_id'], body['source_id'], identity_hash,
                     body['source_sequence'], body['event_id']))
            return _public(event)

    def observation_status(self, principal, project_id, task_id):
        """Read cached evidence, with no probes or freshness/death inference."""
        from .store import _identifier, _public
        _identifier(task_id, 'task_id')
        with self._connection() as connection:
            self._authorize(connection, principal, project_id, 'tasks:read')
            self._task(connection, project_id, task_id)
            return _public(connection.execute('SELECT e.* FROM observation_projections p JOIN observation_events e '
                'ON e.project_id = p.project_id AND e.event_id = p.event_id WHERE p.project_id = %s '
                'AND p.task_id = %s ORDER BY p.component_id, p.source_id', (project_id, task_id)).fetchall())


class FakeObserver:
    """Caller-owned packets for deterministic replay tests, not an offline host spool."""
    def __init__(self, packets):
        from copy import deepcopy
        self.packets = deepcopy(packets)

    def replay(self, store, principal, project_id):
        return [store.record_observation(principal, project_id, packet) for packet in self.packets]
