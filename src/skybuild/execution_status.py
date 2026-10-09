"""Read one task's recorded execution evidence without probing or changing it."""
from .contracts import DomainError


# Each section has an independent SQL bound. One statement gives all returned
# records the same MVCC snapshot, even at the default READ COMMITTED isolation.
SNAPSHOT = """
SELECT t.task_id, t.revision,
       (SELECT row_to_json(c) FROM (
           SELECT fence, holder, task_revision, lease_until, held
           FROM task_claims
           WHERE project_id = %(project)s AND task_id = %(task)s
       ) c) AS claim,
       COALESCE((SELECT json_agg(r ORDER BY r.action_id) FROM (
           SELECT action_id, attempt_id, actor, claim_fence, task_revision, units, state
           FROM cpu_reservations
           WHERE project_id = %(project)s AND task_id = %(task)s
           ORDER BY action_id LIMIT %(fetch)s
       ) r), '[]'::json) AS reservations,
       COALESCE((SELECT json_agg(f ORDER BY f.operation_id) FROM (
           SELECT operation_id, attempt_id, task_revision, state, exposure_held, created_at
           FROM task_effects
           WHERE project_id = %(project)s AND task_id = %(task)s
           ORDER BY operation_id LIMIT %(fetch)s
       ) f), '[]'::json) AS effects,
       COALESCE((SELECT json_agg(o ORDER BY o.component_id, o.source_id) FROM (
           SELECT e.event_id, e.attempt_id, p.component_id, p.source_id,
                  e.source_sequence, e.observed_at, e.received_at, e.state
           FROM observation_projections p
           JOIN observation_events e ON e.project_id = p.project_id AND e.event_id = p.event_id
           WHERE p.project_id = %(project)s AND p.task_id = %(task)s
             AND e.project_id = %(project)s AND e.task_id = %(task)s
           ORDER BY p.component_id, p.source_id LIMIT %(fetch)s
       ) o), '[]'::json) AS observations
FROM tasks t WHERE t.project_id = %(project)s AND t.task_id = %(task)s
"""


class ExecutionStatus:
    def execution_status(self, principal, project_id, task_id, *, limit=20):
        """Return bounded cached records, never an overall safe/idle verdict.

        An absent claim means no recorded claim. Empty pages mean no records
        observed in this snapshot, not proof that processes or effects are absent.
        Truncation does not mean remaining exposure has cleared. Observation
        reports never override held claims, reservations, or effect exposure.
        """
        from .store import _identifier, _public
        _identifier(task_id, 'task_id')
        self._page(limit, 0)
        with self._connection() as connection:
            # Recheck current grants before revealing whether the task exists.
            self._authorize(connection, principal, project_id, 'tasks:read')
            row = connection.execute(SNAPSHOT, {
                'project': project_id, 'task': task_id, 'fetch': limit + 1,
            }).fetchone()
            if row is None:
                raise DomainError('not_found', 'Task not found', 404)
            result = {'task_id': row['task_id'], 'revision': row['revision'], 'claim': row['claim']}
            for section in ('reservations', 'effects', 'observations'):
                items = row[section]
                result[section] = {'items': items[:limit], 'truncated': len(items) > limit}
            return _public(result)
