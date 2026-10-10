"""Dedicated PostgreSQL persistence. Imports never connect or migrate."""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import re
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .contracts import DomainError, Principal, valid_identifier
from .claims import Claims
from .admission import CPUAdmission
from .observations import Observations
from .execution_status import ExecutionStatus
from .board import BoardQueries


OPERATIONS = frozenset({'tasks:read', 'tasks:write', 'tasks:claim', 'cord:send', 'cord:read', 'cord:handle'})
TASK_FIELDS = frozenset({
    'title', 'description', 'status', 'priority', 'dependencies', 'acceptance_criteria',
    'architecture_refs', 'assignee', 'phase', 'next_action', 'blocker', 'responsible', 'metadata',
})
STATUSES = frozenset({'proposed', 'ready', 'in-progress', 'blocked', 'deferred', 'done', 'superseded'})
TASK_SELECT = ('SELECT *, ARRAY(SELECT dependency_id FROM task_dependencies d WHERE '
               'd.project_id = tasks.project_id AND d.task_id = tasks.task_id ORDER BY dependency_id) AS dependencies '
               'FROM tasks ')


def _invalid(message):
    raise DomainError('validation', message, 422)


def _text(value, name, maximum=200, *, nullable=False, empty=False):
    if nullable and value is None:
        return None
    if not isinstance(value, str) or len(value) > maximum or '\x00' in value:
        _invalid(f'{name} must be text of at most {maximum} characters')
    try:
        value.encode('utf-8')
    except UnicodeEncodeError:
        _invalid(f'{name} must contain valid Unicode')
    if not empty and not value.strip():
        _invalid(f'{name} must not be empty')
    return value


def _json(value):
    pending, visited = [(value, 0)], 0
    while pending:
        item, depth = pending.pop()
        visited += 1
        if depth > 100 or visited + len(pending) > 100000:
            _invalid('JSON exceeds nesting or item limits')
        if isinstance(item, str) and '\x00' in item:
            _invalid('JSON must not contain null characters')
        if isinstance(item, (dict, list, tuple)):
            size = len(item) * (2 if isinstance(item, dict) else 1)
            if visited + len(pending) + size > 100000:
                _invalid('JSON exceeds nesting or item limits')
            children = (*item.keys(), *item.values()) if isinstance(item, dict) else item
            pending.extend((child, depth + 1) for child in children)
    try:
        result = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
        result.encode('utf-8')
        return result
    except (TypeError, ValueError, RecursionError, UnicodeEncodeError):
        _invalid('Body must contain valid JSON values')


def _identifier(value, name):
    value = _text(value, name)
    if not valid_identifier(value):
        _invalid(f'{name} contains unsafe path characters')
    return value


def _body(value, fields):
    if not isinstance(value, dict) or set(value) - fields:
        _invalid('Body contains unknown or immutable fields')
    if len(_json(value).encode()) > 65536:
        _invalid('Body exceeds 64 KiB')


MANAGED_METADATA_KEYS = frozenset({'_skybuild_workflow', '_skybuild_completion'})
USER_METADATA_LIMIT = 16 * 1024
MANAGED_METADATA_LIMIT = 64 * 1024


def _validate_metadata(value):
    """Keep caller data bounded separately from internally managed evidence.

    Public mutations reject reserved keys before reaching this shared validator.
    Nested workflow records keep their own 16 KiB limits. The larger aggregate
    budget lets valid user data coexist with bounded task and acceptance records.
    """
    if not isinstance(value, dict):
        _invalid('metadata must be an object')
    user = {key: item for key, item in value.items() if key not in MANAGED_METADATA_KEYS}
    if len(_json(user).encode()) > USER_METADATA_LIMIT:
        _invalid('User metadata must be an object of at most 16 KiB')
    if len(_json(value).encode()) > MANAGED_METADATA_LIMIT:
        _invalid('Managed task metadata exceeds 64 KiB')
    for key in MANAGED_METADATA_KEYS & value.keys():
        if not isinstance(value[key], dict):
            _invalid('Reserved task metadata must contain objects')
    workflow = value.get('_skybuild_workflow', {})
    if 'generation' in workflow and (type(workflow['generation']) is not int or
                                     not 0 <= workflow['generation'] < 2**63):
        _invalid('Workflow generation must be a nonnegative bigint')
    if 'petri' in workflow:
        from .workflow import TaskToken
        petri = workflow['petri']
        if (not isinstance(petri, dict) or type(petri.get('schema_version')) is not int
                or petri['schema_version'] != 1 or not isinstance(petri.get('token'), dict)):
            _invalid('Petri metadata requires a versioned task token')
        TaskToken.from_dict(petri['token'])
    return value


def _public(value):
    if isinstance(value, dict):
        return {key: _public(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_public(item) for item in value]
    if isinstance(value, (datetime, UUID)):
        return value.isoformat() if isinstance(value, datetime) else str(value)
    return value


class Store(Claims, CPUAdmission, Observations, ExecutionStatus, BoardQueries):
    def __init__(self, dsn: str, expected_database: str, expected_system_identifier: str | None = None):
        self.dsn = dsn
        self.expected_database = _text(expected_database, 'expected_database', 63)
        if expected_system_identifier is not None and (
                not isinstance(expected_system_identifier, str)
                or not expected_system_identifier.isdecimal() or len(expected_system_identifier) > 32):
            raise ValueError('expected_system_identifier must be a decimal PostgreSQL system identifier')
        self.expected_system_identifier = expected_system_identifier

    @contextmanager
    def _connection(self, *, consistent_snapshot=False):
        try:
            with psycopg.connect(self.dsn, connect_timeout=5, row_factory=dict_row) as connection:
                if consistent_snapshot:
                    connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                if self.expected_system_identifier is None:
                    identity = connection.execute('SELECT current_database() AS name').fetchone()
                else:
                    identity = connection.execute(
                        'SELECT current_database() AS name, '
                        '(SELECT system_identifier::text FROM pg_control_system()) AS system_identifier'
                    ).fetchone()
                if identity['name'] != self.expected_database:
                    raise DomainError('database_identity', 'Database identity does not match configuration', 503)
                if (self.expected_system_identifier is not None
                        and identity['system_identifier'] != self.expected_system_identifier):
                    raise DomainError('database_identity', 'PostgreSQL cluster identity does not match configuration', 503)
                connection.execute('SET LOCAL search_path TO skybuild, pg_catalog')
                connection.execute("SET LOCAL statement_timeout = '10s'")
                connection.execute("SET LOCAL lock_timeout = '5s'")
                yield connection
        except psycopg.Error as error:
            raise DomainError('unavailable', 'Database operation unavailable', 503) from error

    def migrate(self) -> None:
        with self._connection() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended('skybuild:migrate', 0))")
            connection.execute('CREATE SCHEMA IF NOT EXISTS skybuild')
            connection.execute('CREATE TABLE IF NOT EXISTS schema_migrations (version integer PRIMARY KEY, digest text NOT NULL)')
            for path in sorted(Path(__file__).with_name('migrations').glob('*.sql')):
                version = int(path.name.split('_', 1)[0])
                source = path.read_text()
                digest = hashlib.sha256(source.encode()).hexdigest()
                applied = connection.execute('SELECT digest FROM schema_migrations WHERE version = %s', (version,)).fetchone()
                if applied:
                    if applied['digest'] != digest:
                        raise DomainError('schema_mismatch', 'Applied migration differs from source', 503)
                    continue
                connection.execute(source)
                connection.execute('INSERT INTO schema_migrations VALUES (%s, %s)', (version, digest))

    def readiness(self) -> dict:
        with self._connection() as connection:
            versions = connection.execute('SELECT version, digest FROM schema_migrations ORDER BY version').fetchall()
            expected = [
                {'version': int(path.name.split('_', 1)[0]), 'digest': hashlib.sha256(path.read_bytes()).hexdigest()}
                for path in sorted(Path(__file__).with_name('migrations').glob('*.sql'))
            ]
            if not expected or versions != expected:
                raise DomainError('schema_mismatch', 'Database schema is not ready', 503)
            return {'ready': True, 'schema_version': versions[-1]['version']}

    def provision_principal(self, principal_id, token, *, is_admin=False, grants=None) -> None:
        principal_id = _identifier(principal_id, 'principal_id')
        _text(token, 'token', 4096)
        if len(token) < 32:
            _invalid('Credentials must have at least 32 characters of high-entropy secret material')
        if not isinstance(is_admin, bool) or (grants is not None and not isinstance(grants, dict)):
            _invalid('Invalid principal configuration')
        rows = []
        for project_id, operations in (grants or {}).items():
            _identifier(project_id, 'project_id')
            if not isinstance(operations, (list, tuple, set, frozenset)) or not all(isinstance(item, str) and item in OPERATIONS for item in operations):
                _invalid('Unknown project operation')
            rows.extend((principal_id, project_id, operation) for operation in set(operations))
        with self._connection() as connection:
            try:
                connection.execute(
                    'INSERT INTO principals VALUES (%s, %s, %s) ON CONFLICT (principal_id) DO UPDATE '
                    'SET token_verifier = EXCLUDED.token_verifier, is_admin = EXCLUDED.is_admin',
                    (principal_id, hashlib.sha256(token.encode()).hexdigest(), is_admin),
                )
            except psycopg.errors.UniqueViolation:
                raise DomainError('conflict', 'Credential is already assigned', 409) from None
            connection.execute('DELETE FROM principal_grants WHERE principal_id = %s', (principal_id,))
            if rows:
                with connection.cursor() as cursor:
                    cursor.executemany('INSERT INTO principal_grants VALUES (%s, %s, %s)', rows)

    def _principal(self, connection, principal_id):
        row = connection.execute('SELECT principal_id, is_admin FROM lock_principal(%s)', (principal_id,)).fetchone()
        if not row:
            raise DomainError('authentication', 'Invalid credentials', 401)
        grants = {}
        for grant in connection.execute('SELECT project_id, operation FROM principal_grants WHERE principal_id = %s', (principal_id,)).fetchall():
            grants.setdefault(grant['project_id'], set()).add(grant['operation'])
        return Principal(row['principal_id'], row['is_admin'], {key: frozenset(value) for key, value in grants.items()})

    def authenticate(self, token: str) -> Principal:
        if not isinstance(token, str) or not 32 <= len(token) <= 4096:
            raise DomainError('authentication', 'Invalid credentials', 401)
        with self._connection() as connection:
            row = connection.execute('SELECT principal_id FROM principals WHERE token_verifier = %s', (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
            if not row:
                raise DomainError('authentication', 'Invalid credentials', 401)
            return self._principal(connection, row['principal_id'])

    def _authorize(self, connection, principal, project_id, operation):
        _identifier(project_id, 'project_id')
        if not isinstance(principal, Principal):
            raise DomainError('authentication', 'Invalid credentials', 401)
        current = self._principal(connection, principal.principal_id)
        if not current.is_admin and operation not in current.grants.get(project_id, ()):
            raise DomainError('authorization', 'Project operation not permitted', 403)
        if operation == 'tasks:write' and connection.execute(
            "SELECT 1 WHERE lock_ledger_import(%s)", (project_id,)
        ).fetchone():
            raise DomainError('authority', 'Markdown ledger remains task authority', 409)
        return current

    @staticmethod
    def _page(limit, offset):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            _invalid('Pagination requires limit 1–100 and nonnegative offset')

    def _idempotent(self, connection, principal, project_id, operation, key, payload, mutation):
        _text(key, 'idempotency_key')
        scope = (principal.principal_id, project_id, operation, key)
        connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))', (_json(scope),))
        digest = hashlib.sha256(_json(payload).encode()).hexdigest()
        prior = connection.execute(
            'SELECT payload_hash, response FROM idempotency WHERE principal_id = %s AND project_id = %s AND operation = %s AND idempotency_key = %s', scope,
        ).fetchone()
        if prior:
            if prior['payload_hash'] != digest:
                raise DomainError('idempotency_conflict', 'Idempotency key was used for different input', 409)
            return prior['response']
        result = _public(mutation())
        connection.execute('INSERT INTO idempotency (principal_id, project_id, operation, idempotency_key, payload_hash, response) VALUES (%s, %s, %s, %s, %s, %s)', (*scope, digest, Jsonb(result)))
        return result

    @staticmethod
    def _graph_lock(connection, project_id):
        connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))', ('skybuild:dependencies:' + project_id,))

    @staticmethod
    def _task(connection, project_id, task_id, *, lock=False):
        _identifier(task_id, 'task_id')
        query = TASK_SELECT + 'WHERE project_id = %s AND task_id = %s' + (' FOR UPDATE' if lock else '')
        row = connection.execute(query, (project_id, task_id)).fetchone()
        if not row:
            raise DomainError('not_found', 'Task not found', 404)
        return _public(row)

    @staticmethod
    def _task_values(body, before=None):
        values = {
            'status': 'proposed', 'priority': 0, 'dependencies': [], 'acceptance_criteria': [],
            'architecture_refs': [], 'assignee': None, 'phase': 'triage',
            'next_action': 'Review task and determine next step', 'blocker': None,
            'responsible': 'owner', 'metadata': {},
        }
        if before:
            values.update({key: before[key] for key in TASK_FIELDS})
        values.update(body)
        for field, maximum in {'title': 500, 'description': 32768, 'phase': 200, 'responsible': 200}.items():
            values[field] = _text(values.get(field), field, maximum)
        for field in ('assignee', 'next_action', 'blocker'):
            values[field] = _text(values[field], field, 4096, nullable=True, empty=True)
        if not isinstance(values['status'], str) or values['status'] not in STATUSES:
            _invalid('Unknown task status')
        if type(values['priority']) is not int or not -(2**31) <= values['priority'] < 2**31:
            _invalid('priority must be a 32-bit integer')
        for field in ('dependencies', 'acceptance_criteria', 'architecture_refs'):
            items = values[field]
            if not isinstance(items, list) or len(items) > 100:
                _invalid(f'{field} must be an array of at most 100 strings')
            for item in items:
                if field == 'dependencies':
                    _identifier(item, field)
                else:
                    _text(item, field, 4096)
            if field == 'dependencies':
                values[field] = sorted(set(items))
        _validate_metadata(values['metadata'])
        if values['status'] != 'done' and not (str(values['next_action'] or '').strip() or str(values['blocker'] or '').strip()):
            _invalid('Unfinished tasks need a next action or blocker')
        return values

    def _replace_task(self, connection, principal, project_id, task_id, before, changes, *, operation, reason):
        self._require_no_effect_exposure(connection, project_id, task_id)
        values = self._task_values(changes, before)
        preserve_acceptance = False
        if self._petri(before) and operation == 'reassess':
            from .completion import current_completion
            preserve_acceptance = current_completion(before)
            if preserve_acceptance:
                values['metadata']['_skybuild_workflow']['generation'] = before['metadata']['_skybuild_workflow'].get('generation', 0)
                values.update(blocker=before['blocker'], next_action=before['next_action'])
        self._advance_readiness(connection, project_id, task_id, values,
                                acknowledge=operation in {'ready', 'completed', 'rework', 'reassess', 'resume'},
                                preserve_input=operation == 'completed' or preserve_acceptance)
        from .reconciliation import synchronize_token
        synchronize_token(before, values, operation=operation, reason=reason)
        # Synchronization adds retained token fields to the same metadata budget.
        self._task_values(values, before)
        columns = [key for key in values if key != 'dependencies']
        parameters = [Jsonb(values[key]) if key == 'metadata' else values[key] for key in columns]
        assignments = sql.SQL(', ').join(sql.SQL('{} = %s').format(sql.Identifier(key)) for key in columns)
        connection.execute(sql.SQL('UPDATE tasks SET {}, revision = revision + 1, updated_at = now() WHERE project_id = %s AND task_id = %s').format(assignments), (*parameters, project_id, task_id))
        self._dependencies(connection, project_id, task_id, values['dependencies'])
        after = self._task(connection, project_id, task_id)
        self._journal(connection, principal, after, before, operation=operation, reason=reason)
        if not preserve_acceptance:
            self._invalidate_dependents(connection, principal, project_id, task_id)
        return after

    @staticmethod
    def _advance_readiness(connection, project_id, task_id, values, *, acknowledge=False, preserve_input=False):
        connection.execute('INSERT INTO task_readiness (project_id, task_id) VALUES (%s, %s) ON CONFLICT DO NOTHING',
                           (project_id, task_id))
        row = connection.execute(
            'UPDATE task_readiness SET input_generation = input_generation + %s, '
            'assessed_generation = CASE WHEN %s THEN input_generation + %s ELSE assessed_generation END '
            'WHERE project_id = %s AND task_id = %s RETURNING input_generation, assessed_generation',
            (0 if preserve_input else 1, acknowledge, 0 if preserve_input else 1, project_id, task_id)).fetchone()
        metadata = json.loads(json.dumps(values['metadata']))
        workflow = metadata.setdefault('_skybuild_workflow', {})
        workflow.setdefault('generation', 0)
        workflow['readiness'] = dict(row)
        values['metadata'] = metadata

    def _invalidate_dependents(self, connection, principal, project_id, task_id):
        # The graph lock covers discovery, generation changes and all journal writes.
        rows = connection.execute(
            'WITH RECURSIVE edges(task_id, dependency_id) AS ('
            'SELECT task_id, dependency_id FROM task_dependencies WHERE project_id = %s UNION '
            "SELECT task_id, metadata->'_skybuild_workflow'->'deferral'->>'milestone_task_id' FROM tasks "
            "WHERE project_id = %s AND metadata->'_skybuild_workflow'->'deferral'->>'milestone_task_id' IS NOT NULL), "
            'affected(task_id) AS (SELECT task_id FROM edges WHERE dependency_id = %s UNION '
            'SELECT e.task_id FROM edges e JOIN affected a ON e.dependency_id = a.task_id) '
            'SELECT task_id FROM affected WHERE task_id <> %s ORDER BY task_id',
            (project_id, project_id, task_id, task_id)).fetchall()
        for row in rows:
            dependent_id = row['task_id']
            before = self._task(connection, project_id, dependent_id, lock=True)
            if before['status'] == 'superseded':
                continue
            self._require_no_effect_exposure(connection, project_id, dependent_id)
            values = self._task_values({}, before)
            metadata = json.loads(json.dumps(before['metadata']))
            workflow = metadata.setdefault('_skybuild_workflow', {})
            workflow['generation'] = workflow.get('generation', 0) + 1
            workflow.update({'last_action': 'dependency_invalidated', 'reason': f'Dependency {task_id} changed'})
            values['metadata'] = metadata
            if before['status'] != 'deferred':
                values.update({'status': 'blocked', 'phase': 'reassess',
                               'blocker': f'Dependency {task_id} changed',
                               'next_action': 'Reconcile active effects and reassess dependencies' if
                               self._has_started_history(connection, project_id, dependent_id) else
                               'Reassess current definition and dependencies'})
            self._advance_readiness(connection, project_id, dependent_id, values)
            from .reconciliation import synchronize_token
            synchronize_token(before, values, operation='dependency_invalidated',
                              reason=f'Dependency {task_id} changed')
            self._task_values(values, before)
            columns = [key for key in values if key != 'dependencies']
            assignments = sql.SQL(', ').join(sql.SQL('{} = %s').format(sql.Identifier(key)) for key in columns)
            parameters = [Jsonb(values[key]) if key == 'metadata' else values[key] for key in columns]
            connection.execute(sql.SQL('UPDATE tasks SET {}, revision = revision + 1, updated_at = now() '
                                       'WHERE project_id = %s AND task_id = %s').format(assignments),
                               (*parameters, project_id, dependent_id))
            after = self._task(connection, project_id, dependent_id)
            self._journal(connection, principal, after, before, operation='dependency_invalidated',
                          reason=f'Dependency {task_id} changed')

    @staticmethod
    def _dependencies(connection, project_id, task_id, dependencies):
        if task_id in dependencies:
            _invalid('A task cannot depend on itself')
        found = connection.execute('SELECT task_id, status FROM tasks WHERE project_id = %s AND task_id = ANY(%s)', (project_id, dependencies)).fetchall()
        if len(found) != len(dependencies):
            _invalid('Dependencies must refer to existing tasks in this project')
        existing = {row['dependency_id'] for row in connection.execute(
            'SELECT dependency_id FROM task_dependencies WHERE project_id = %s AND task_id = %s',
            (project_id, task_id)).fetchall()}
        if any(row['status'] == 'superseded' and row['task_id'] not in existing for row in found):
            raise DomainError('workflow_conflict', 'New dependencies cannot target superseded tasks', 409)
        cycle = connection.execute(
            'WITH RECURSIVE reachable(task_id) AS (SELECT unnest(%s::text[]) UNION '
            'SELECT d.dependency_id FROM task_dependencies d JOIN reachable r ON d.task_id = r.task_id WHERE d.project_id = %s) '
            'SELECT 1 FROM reachable WHERE task_id = %s LIMIT 1', (dependencies, project_id, task_id),
        ).fetchone()
        if cycle:
            raise DomainError('dependency_cycle', 'Dependencies would create a cycle', 409)
        connection.execute('DELETE FROM task_dependencies WHERE project_id = %s AND task_id = %s', (project_id, task_id))
        for dependency in dependencies:
            connection.execute('INSERT INTO task_dependencies VALUES (%s, %s, %s)', (project_id, task_id, dependency))

    @staticmethod
    def _journal(connection, principal, after, before=None, *, operation=None, reason=None, event_facts=None):
        operation = operation or ('updated' if before else 'created')
        if event_facts is not None and (not isinstance(event_facts, dict) or len(_json(event_facts).encode()) > 65536):
            _invalid('Workflow journal facts must be an object of at most 64 KiB')
        if operation == 'created':
            connection.execute('INSERT INTO task_readiness (project_id, task_id) VALUES (%s, %s) ON CONFLICT DO NOTHING',
                               (after['project_id'], after['task_id']))
        connection.execute(
            'INSERT INTO task_journal (event_id, project_id, task_id, actor, operation, revision, reason, before_state, after_state, event_facts) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)',
            (uuid4(), after['project_id'], after['task_id'], principal.principal_id, operation,
             after['revision'], reason or 'Task ' + operation, Jsonb(before) if before else None, Jsonb(after),
             Jsonb(event_facts) if event_facts is not None else None),
        )

    def create_task(self, principal, project_id, body: dict, idempotency_key: str) -> dict:
        _body(body, TASK_FIELDS | {'task_id'})
        if body.get('status', 'proposed') != 'proposed' or body.get('phase', 'triage') != 'triage' or body.get('blocker') is not None:
            _invalid('New tasks start in proposed triage; use guarded actions for workflow changes')
        if isinstance(body.get('metadata'), dict) and set(body['metadata']) & {'_skybuild_workflow', '_skybuild_completion'}:
            _invalid('Workflow metadata is managed by task actions')
        task_id = _identifier(body.get('task_id'), 'task_id')
        values = self._task_values({key: value for key, value in body.items() if key != 'task_id'})
        with self._connection() as connection:
            principal = self._authorize(connection, principal, project_id, 'tasks:write')
            def mutation():
                self._graph_lock(connection, project_id)
                if connection.execute('SELECT 1 FROM tasks WHERE project_id = %s AND task_id = %s', (project_id, task_id)).fetchone():
                    raise DomainError('conflict', 'Task already exists', 409)
                columns = [key for key in values if key != 'dependencies']
                parameters = [Jsonb(values[key]) if key == 'metadata' else values[key] for key in columns]
                connection.execute(sql.SQL('INSERT INTO tasks (project_id, task_id, {}) VALUES (%s, %s, {})').format(sql.SQL(', ').join(map(sql.Identifier, columns)), sql.SQL(', ').join(sql.Placeholder() for _ in columns)), (project_id, task_id, *parameters))
                self._dependencies(connection, project_id, task_id, values['dependencies'])
                result = self._task(connection, project_id, task_id)
                metadata = self._new_task_metadata(result)
                place = metadata.get('_skybuild_workflow', {}).get('petri', {}).get('token', {}).get('place')
                # Ready remains readable by the existing dispatcher and assignment
                # envelope. This definition assessment grants no launch permission.
                compatibility_status = 'ready' if place == 'ready' else result['status']
                compatibility_phase = 'ready' if place == 'ready' else result['phase']
                connection.execute('UPDATE tasks SET metadata = %s, status = %s, phase = %s '
                                   'WHERE project_id = %s AND task_id = %s',
                                   (Jsonb(metadata), compatibility_status, compatibility_phase, project_id, task_id))
                result = self._task(connection, project_id, task_id)
                self._journal(connection, principal, result)
                if self._petri(result) and self.workflow_token(result).place.value == 'ready':
                    connection.execute('UPDATE task_readiness SET assessed_generation = input_generation '
                                       'WHERE project_id = %s AND task_id = %s', (project_id, task_id))
                return result
            return self._idempotent(connection, principal, project_id, 'task.create', idempotency_key, body, mutation)

    @staticmethod
    def _new_task_metadata(task):
        """Enroll new tasks. Legacy test fixtures replace only this pure helper."""
        from .enrollment import enrollment_token, install_token
        metadata = json.loads(json.dumps(task['metadata']))
        token = enrollment_token(task, input_generation=1, revision=task['revision'], new=True)
        install_token(metadata, token)
        metadata['_skybuild_workflow']['readiness'] = {
            'input_generation': 1, 'assessed_generation': 1 if token.place.value == 'ready' else 0}
        _validate_metadata(metadata)
        return metadata

    def list_tasks(self, principal, project_id, *, limit=100, offset=0, after_task_id=None, by_id=False) -> list[dict]:
        self._page(limit, offset)
        if type(by_id) is not bool:
            _invalid('Invalid task list order')
        if after_task_id is not None:
            _identifier(after_task_id, 'after_task_id')
        if (by_id or after_task_id is not None) and offset:
            _invalid('Cursor and offset pagination cannot be combined')
        with self._connection() as connection:
            self._authorize(connection, principal, project_id, 'tasks:read')
            if after_task_id is not None:
                return _public(connection.execute(TASK_SELECT + 'WHERE project_id = %s AND task_id > %s ORDER BY task_id LIMIT %s',
                                                  (project_id, after_task_id, limit)).fetchall())
            if by_id:
                return _public(connection.execute(TASK_SELECT + 'WHERE project_id = %s ORDER BY task_id LIMIT %s',
                                                  (project_id, limit)).fetchall())
            return _public(connection.execute(TASK_SELECT + 'WHERE project_id = %s ORDER BY priority, task_id LIMIT %s OFFSET %s', (project_id, limit, offset)).fetchall())

    @staticmethod
    def _petri(task):
        value = task.get('metadata', {}).get('_skybuild_workflow', {}).get('petri')
        return isinstance(value, dict) and value.get('schema_version') == 1

    @staticmethod
    def workflow_token(task):
        """Project current task fields over persisted workflow attributes."""
        from .workflow import TaskToken
        workflow = task.get('metadata', {}).get('_skybuild_workflow', {})
        petri = workflow.get('petri', {})
        if petri.get('schema_version') != 1:
            raise DomainError('workflow_conflict', 'Task has no Petri workflow', 409)
        body = dict(petri['token'])
        body.update(project_id=task['project_id'], task_id=task['task_id'], title=task['title'],
                    priority=task['priority'], dependencies=list(task['dependencies']),
                    responsible=task['responsible'], next_action=task['next_action'] or '',
                    blocker=task['blocker'], revision=task['revision'],
                    superseded=task['status'] == 'superseded')
        return TaskToken.from_dict(body)

    def _workflow_context(self, connection, principal, task):
        """Read trusted transaction facts. Missing producer evidence fails closed."""
        token = self.workflow_token(task)
        context = {name: getattr(token, name) for name in
                   ('source_head', 'target_base', 'definition_revision', 'input_generation', 'policy_version')}
        readiness = connection.execute('SELECT * FROM task_readiness WHERE project_id = %s AND task_id = %s',
                                       (task['project_id'], task['task_id'])).fetchone()
        context['definition_sufficient'] = bool(task.get('title') and task.get('description') and task.get('acceptance_criteria'))
        context['current_inputs'] = bool(context['definition_sufficient'] and readiness
                                         and readiness['input_generation'] == token.input_generation
                                         and readiness['assessed_generation'] == token.input_generation)
        context['effects_resolved'] = not connection.execute(
            "SELECT 1 FROM task_effects WHERE project_id = %s AND task_id = %s AND exposure_held "
            "UNION ALL SELECT 1 FROM cpu_reservations WHERE project_id = %s AND task_id = %s AND state = 'reserved' LIMIT 1",
            (task['project_id'], task['task_id'], task['project_id'], task['task_id'])).fetchone()
        claim = connection.execute('SELECT *, lease_until > clock_timestamp() AS live FROM task_claims '
                                   'WHERE project_id = %s AND task_id = %s FOR UPDATE',
                                   (task['project_id'], task['task_id'])).fetchone()
        context['claim_live'] = bool(claim and claim['held'] and claim['live']
                                     and claim['holder'] == principal.principal_id
                                     and claim['fence'] == token.claim_fence)
        context.update(attempt_id=token.attempt_id, claim_fence=token.claim_fence,
                       responsible=principal.principal_id, control_authorized=principal.is_admin,
                       now=datetime.now(timezone.utc))
        context['dependencies_satisfied'] = True
        try:
            self._require_current_dependencies(connection, task['project_id'], task)
        except DomainError as error:
            if error.code != 'workflow_conflict':
                raise
            context['dependencies_satisfied'] = False
        return context

    @staticmethod
    def workflow_projection(task):
        """Pure additive fields. Action permission needs a trusted Store read."""
        if not Store._petri(task):
            return {'place': None, 'validation': [], 'enabled_actions': [], 'evidence_freshness': 'unavailable'}
        token = Store.workflow_token(task)
        fresh = all((result.source_head, result.target_base, result.definition_revision,
                     result.input_generation, result.policy_version, result.attempt_id, result.claim_fence) ==
                    (token.source_head, token.target_base, token.definition_revision,
                     token.input_generation, token.policy_version, token.attempt_id, token.claim_fence)
                    for result in token.evidence)
        return {'place': token.place.value, 'validation': [result.to_dict() for result in token.evidence],
                'enabled_actions': [], 'evidence_freshness': 'current' if token.evidence and fresh else
                'stale' if token.evidence else 'unavailable'}

    def _workflow_control_context(self, connection, principal, task, context):
        """Control requests wait for held ownership, including an expired claim.

        Worker submission retains its separate fenced claim checks. This helper
        changes only owner control facts and grants no execution permission.
        """
        checked = dict(context)
        if connection.execute('SELECT 1 FROM task_claims WHERE project_id = %s AND task_id = %s AND held',
                              (task['project_id'], task['task_id'])).fetchone():
            checked['effects_resolved'] = False
        if principal.is_admin:
            marker = connection.execute('SELECT input_generation FROM task_readiness WHERE project_id = %s AND task_id = %s',
                                        (task['project_id'], task['task_id'])).fetchone()
            checked['current_inputs'] = bool(marker and marker['input_generation'] == self.workflow_token(task).input_generation)
        return checked

    def _claim_preview_context(self, connection, principal, task, context):
        """Preview ownership only. Recompute all guards in the claim transaction.

        The fixed attempt label and next fence are prospective display facts.
        This read neither allocates an attempt nor grants execution admission.
        """
        from .workflow import Place
        token = self.workflow_token(task)
        checked = {**context, 'admission_permitted': False}
        if (token.place != Place.READY or token.pending_action is not None or token.superseded
                or not (principal.is_admin or 'tasks:claim' in principal.grants.get(task['project_id'], ()))):
            return checked
        try:
            self._require_api_authority(connection, task['project_id'])
            claim = connection.execute('SELECT * FROM task_claims WHERE project_id = %s AND task_id = %s',
                                       (task['project_id'], task['task_id'])).fetchone()
            next_fence = self._require_claim_eligible(connection, task['project_id'], task, claim)
        except DomainError as error:
            if error.code not in {'authority', 'claim_conflict', 'workflow_conflict', 'effect_conflict', 'capacity_conflict'}:
                raise
            return checked
        return {**checked, 'admission_permitted': True, 'claim_live': True,
                'attempt_id': 'claim-capability-preview', 'claim_fence': next_fence,
                'responsible': principal.principal_id}

    def _workflow_view(self, connection, principal, task):
        from .workflow import TaskWorkflow, TRANSITIONS
        token = self.workflow_token(task)
        context = self._workflow_context(connection, principal, task)
        names = {'hold', 'defer', 'release_hold', 'resume_deferred', 'reopen', 'update_control'}
        preview = self._claim_preview_context(connection, principal, task, context)
        enabled = tuple(name for name in TaskWorkflow().enabled(token, preview) if name not in names)
        controls = self._workflow_control_context(connection, principal, task, context)
        enabled += tuple(name for name in TaskWorkflow().enabled(token, controls) if name in names)
        return {'task': {**task, **self.workflow_projection(task), 'enabled_actions': list(enabled)},
                'token': token.to_dict(), 'available_actions': list(enabled),
                'transitions': [spec.to_dict() for spec in TRANSITIONS],
                'disabled_actions': {spec.event: 'Required state, permission or current evidence is unavailable'
                                     for spec in TRANSITIONS if spec.event not in enabled}}

    def task_workflow(self, principal, project_id, task_id):
        with self._connection() as connection:
            principal = self._authorize(connection, principal, project_id, 'tasks:read')
            self._graph_lock(connection, project_id)
            return self._workflow_view(connection, principal, self._task(connection, project_id, task_id, lock=True))

    def initialize_workflow(self, principal, project_id, task_id, expected_revision, idempotency_key):
        """Enroll a legacy task conservatively without changing historical events."""
        from .completion import current_completion
        from .workflow import TaskToken, Place
        if type(expected_revision) is not int or not 1 <= expected_revision < 2**63:
            _invalid('Initialization requires a positive expected revision')
        with self._connection() as connection:
            principal = self._authorize(connection, principal, project_id, 'tasks:write')
            if not principal.is_admin:
                raise DomainError('authorization', 'Only owner/admin may initialize workflow', 403)
            def mutation():
                self._graph_lock(connection, project_id)
                task = self._task(connection, project_id, task_id, lock=True)
                if task['revision'] != expected_revision:
                    raise DomainError('stale_revision', 'Task revision has changed', 409)
                if self._petri(task):
                    return self._workflow_view(connection, principal, task)
                self._require_no_effect_exposure(connection, project_id, task_id)
                readiness = connection.execute('SELECT input_generation FROM task_readiness WHERE project_id = %s AND task_id = %s',
                                               (project_id, task_id)).fetchone()
                from .enrollment import enrollment_token, install_token
                token = enrollment_token(task, input_generation=readiness['input_generation'], revision=task['revision'] + 1)
                metadata = json.loads(json.dumps(task['metadata']))
                install_token(metadata, token)
                _validate_metadata(metadata)
                connection.execute('UPDATE tasks SET metadata = %s, revision = revision + 1 WHERE project_id = %s AND task_id = %s',
                                   (Jsonb(metadata), project_id, task_id))
                after = self._task(connection, project_id, task_id)
                self._journal(connection, principal, after, task, operation='workflow_initialized', reason='Initialize Petri workflow')
                if current_completion(task) and not current_completion(after):
                    self._invalidate_dependents(connection, principal, project_id, task_id)
                return self._workflow_view(connection, principal, after)
            return self._idempotent(connection, principal, project_id, 'workflow.initialize', idempotency_key,
                                    {'task_id': task_id, 'revision': expected_revision}, mutation)

    def _apply_workflow_event(self, connection, principal, before, event, verified_context, *, journal_before=None, receipt=None):
        """Internal producer boundary: context comes from trusted adapter validation."""
        from .workflow import TaskWorkflow, Place
        kernel = TaskWorkflow()
        token = self.workflow_token(before)
        after_token = kernel.apply(token, event, verified_context)
        if event['event'] in {'reopen', 'release_hold', 'resume_deferred'}:
            self._require_no_effect_exposure(connection, before['project_id'], before['task_id'])
            values = self._task_values({}, before)
            self._advance_readiness(connection, before['project_id'], before['task_id'], values,
                                    acknowledge=True, preserve_input=event['event'] != 'reopen')
            from dataclasses import replace
            after_token = replace(after_token, input_generation=values['metadata']['_skybuild_workflow']['readiness']['input_generation'])
        facts = kernel.journal_facts(token, after_token, event)
        if receipt is not None:
            facts['author_output_receipt' if event['event'] == 'submit' else 'integration_receipt'] = receipt
        metadata = json.loads(json.dumps(before['metadata']))
        metadata['_skybuild_workflow']['petri']['token'] = after_token.to_dict()
        if event['event'] in {'reopen', 'release_hold', 'resume_deferred'}:
            metadata['_skybuild_workflow']['readiness'] = values['metadata']['_skybuild_workflow']['readiness']
            if event['event'] == 'reopen':
                metadata['_skybuild_workflow']['generation'] = metadata['_skybuild_workflow'].get('generation', 0) + 1
        if after_token.place != token.place:
            metadata['_skybuild_workflow']['petri']['place_entered_at'] = datetime.now(timezone.utc).isoformat()
        if after_token.place == Place.DEFERRED:
            trigger = ({'until': after_token.deferred_until} if after_token.deferred_until else
                       {'milestone_task_id': after_token.milestone_task_id} if after_token.milestone_task_id else {})
            metadata['_skybuild_workflow']['deferral'] = {'reason': after_token.hold_reason, **trigger}
        elif after_token.place in {Place.READY, Place.HOLD}:
            metadata['_skybuild_workflow'].pop('deferral', None)
        if event['event'] == 'claim':
            metadata['_skybuild_workflow']['petri']['attempt_binding'] = {
                'task_revision': after_token.revision, 'input_generation': after_token.input_generation,
                'attempt_id': after_token.attempt_id, 'claim_fence': after_token.claim_fence}
        # Validate the complete metadata budget, including retained legacy data.
        _validate_metadata(metadata)
        status = {Place.READY: 'ready', Place.WORKING: 'in-progress', Place.VALIDATING: 'in-progress',
                  Place.INTEGRATING: 'in-progress', Place.DONE: 'done', Place.DEFERRED: 'deferred', Place.HOLD: 'blocked'}[after_token.place]
        connection.execute('UPDATE tasks SET metadata = %s, status = %s, phase = %s, responsible = %s, '
                           'next_action = %s, blocker = %s, revision = revision + 1, updated_at = now() '
                           'WHERE project_id = %s AND task_id = %s',
                           (Jsonb(metadata), status, after_token.place.value, after_token.responsible,
                            after_token.next_action or ('Resolve workflow task' if status != 'done' else None),
                            after_token.blocker, before['project_id'], before['task_id']))
        after = self._task(connection, before['project_id'], before['task_id'])
        self._journal(connection, principal, after, journal_before or before, operation='workflow.' + facts['event'],
                      reason=event.get('reason') or 'Workflow ' + facts['event'], event_facts=facts)
        if event['event'] == 'reopen':
            self._invalidate_dependents(connection, principal, before['project_id'], before['task_id'])
        return after

    def _submit_workflow_receipt(self, connection, principal, before, body, operation_id):
        """Bind an author's proposed output, never independent acceptance."""
        from dataclasses import replace
        from .workflow import Place, ResultState
        fields = {'source_head', 'target_base', 'source_branch', 'attempt_id', 'claim_fence',
                  'input_generation', 'definition_revision', 'policy_version'}
        _body(body, fields)
        if set(body) != fields:
            _invalid('Submission requires the complete author output receipt')
        for name in ('source_head', 'target_base'):
            if not isinstance(body[name], str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', body[name]):
                _invalid('Submission requires full lowercase Git object IDs')
        branch = _text(body['source_branch'], 'source_branch', 200)
        if (not re.fullmatch(r'refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]*', branch)
                or any(part in branch for part in ('..', '//', '@{'))
                or branch.endswith(('/', '.', '.lock'))):
            _invalid('Submission requires a bounded branch reference')
        token = self.workflow_token(before)
        if token.place != Place.WORKING or token.pending_action is not None:
            raise DomainError('workflow_conflict', 'Submission requires active Working task', 409)
        for name in ('attempt_id', 'claim_fence', 'input_generation', 'definition_revision', 'policy_version'):
            if type(body[name]) is not type(getattr(token, name)) or body[name] != getattr(token, name):
                raise DomainError('stale_evidence', 'Submission does not match current attempt inputs', 409)
        claim = connection.execute('SELECT *, lease_until > clock_timestamp() AS live FROM task_claims '
                                   'WHERE project_id = %s AND task_id = %s FOR UPDATE',
                                   (before['project_id'], before['task_id'])).fetchone()
        if (not claim or not claim['held'] or not claim['live'] or claim['fence'] != token.claim_fence
                or (claim['holder'] != principal.principal_id and not principal.is_admin)):
            raise DomainError('claim_conflict', 'Submission requires current holder or explicit admin attestation', 409)
        context = self._workflow_context(connection, principal, before)
        if not context['current_inputs'] or not context['effects_resolved']:
            raise DomainError('workflow_conflict', 'Submission inputs or effects require reconciliation', 409)
        changed_inputs = (token.source_head, token.target_base) != (body['source_head'], body['target_base'])
        prepared = json.loads(json.dumps(before))
        if changed_inputs:
            readiness = connection.execute(
                'UPDATE task_readiness SET input_generation = input_generation + 1, '
                'assessed_generation = input_generation + 1 WHERE project_id = %s AND task_id = %s '
                'RETURNING input_generation, assessed_generation',
                (before['project_id'], before['task_id'])).fetchone()
            token = replace(token, input_generation=readiness['input_generation'],
                            evidence=tuple(replace(result, state=ResultState.STALE) for result in token.evidence))
            prepared['metadata']['_skybuild_workflow']['readiness'] = dict(readiness)
            prepared['metadata']['_skybuild_workflow']['generation'] = prepared['metadata']['_skybuild_workflow'].get('generation', 0) + 1
        token = replace(token, source_head=body['source_head'], target_base=body['target_base'], source_branch=branch)
        prepared['metadata']['_skybuild_workflow']['petri']['token'] = token.to_dict()
        context.update({name: getattr(token, name) for name in
                        ('source_head', 'target_base', 'definition_revision', 'input_generation', 'policy_version')})
        context.update(submission_verified=True, claim_live=True)
        event = {'event': 'submit', 'operation_id': operation_id, 'expected_revision': before['revision']}
        after = self._apply_workflow_event(connection, principal, prepared, event, context, journal_before=before, receipt=body)
        # The complete receipt is preserved in the task's journal after-state.
        # The workflow token holds its exact proposed head/base/branch and inputs.
        return after

    def workflow_transition(self, principal, project_id, task_id, event, body, expected_revision, idempotency_key):
        from .workflow import WorkflowEvent, _workflow_event
        if event == 'submit':
            _body(body, {'source_head', 'target_base', 'source_branch', 'attempt_id', 'claim_fence',
                         'input_generation', 'definition_revision', 'policy_version'})
            request = _workflow_event({'event': event, 'operation_id': idempotency_key, 'expected_revision': expected_revision})
        else:
            _body(body, set(WorkflowEvent.__annotations__) - {'event', 'operation_id', 'expected_revision'})
            request = _workflow_event({**body, 'event': event, 'operation_id': idempotency_key, 'expected_revision': expected_revision})
        if event in {'claim', 'accept'}:
            raise DomainError('workflow_conflict', 'Use the dedicated claim or completion evidence path', 409)
        with self._connection() as connection:
            principal = self._authorize(connection, principal, project_id, 'tasks:write')
            def mutation():
                self._graph_lock(connection, project_id)
                before = self._task(connection, project_id, task_id, lock=True)
                if before['revision'] != expected_revision:
                    raise DomainError('stale_revision', 'Task revision has changed', 409)
                if event == 'submit':
                    after = self._submit_workflow_receipt(connection, principal, before, body, idempotency_key)
                else:
                    context = self._workflow_context(connection, principal, before)
                    if event in {'hold', 'defer', 'release_hold', 'resume_deferred', 'reopen', 'update_control'} and principal.is_admin:
                        # Control release reassesses a stored generation without execution approval.
                        if event in {'release_hold', 'resume_deferred', 'reopen'}:
                            self._require_no_effect_exposure(connection, project_id, task_id)
                        context = self._workflow_control_context(connection, principal, before, context)
                    if event == 'validation_result' and 'result' in body:
                        from .workflow import ValidationResult, ValidationStage
                        result = ValidationResult.from_dict(body['result'])
                        token = self.workflow_token(before)
                        context['result_authorized'] = (result.producer == principal.principal_id
                            and (principal.is_admin or (context['claim_live'] and result.stage != ValidationStage.CODE_REVIEW))
                            and (result.stage != ValidationStage.CODE_REVIEW or result.producer != token.responsible))
                    after = self._apply_workflow_event(connection, principal, before, request, context)
                return self._workflow_view(connection, principal, after)
            return self._idempotent(connection, principal, project_id, 'workflow.' + event, idempotency_key,
                                    {'task_id': task_id, 'event': request, 'body': body}, mutation)

    def verified_workflow_transition(self, principal, project_id, task_id, event, body,
                                     expected_revision, idempotency_key, *, evidence, verifier):
        """Internal admin-attestation boundary, never an HTTP guard-fact endpoint.

        The adapter validates its receipt inside this transaction. Evidence is
        included in the retry identity. Database input and ownership facts cannot
        be replaced by a receipt. This method never performs an external effect.
        """
        from .workflow import WorkflowEvent, _workflow_event
        _body(body, set(WorkflowEvent.__annotations__) - {'event', 'operation_id', 'expected_revision'})
        _body(evidence, set(evidence) if isinstance(evidence, dict) else set())
        if not callable(verifier) or event == 'claim':
            _invalid('Verified workflow requires an internal receipt verifier')
        request = _workflow_event({**body, 'event': event, 'operation_id': idempotency_key,
                                   'expected_revision': expected_revision})
        with self._connection() as connection:
            principal = self._authorize(connection, principal, project_id, 'tasks:write')
            if not principal.is_admin:
                raise DomainError('authorization', 'Only owner/admin may attest producer evidence', 403)
            def mutation():
                self._graph_lock(connection, project_id)
                before = self._task(connection, project_id, task_id, lock=True)
                if before['revision'] != expected_revision:
                    raise DomainError('stale_revision', 'Task revision has changed', 409)
                context = self._workflow_context(connection, principal, before)
                checked = verifier(connection, before, dict(context), evidence)
                if not isinstance(checked, dict):
                    _invalid('Receipt verifier must return checked facts')
                allowed = {'submission_verified', 'result_authorized', 'validation_verified', 'integration_fixed',
                           'bundle_id', 'publication_required', 'acceptance_verified', 'publication_verified',
                           'task_included', 'policy_reason', 'failure_confirmed', 'completion_evidence',
                           'publication_policy_version', 'acceptance_policy', 'integration_observation_verified',
                           'exclusion_verified', 'publication_outcome'}
                if set(checked) - allowed:
                    _invalid('Receipt cannot replace database input or ownership facts')
                context.update({key: value for key, value in checked.items() if key != 'completion_evidence'})
                if event == 'accept':
                    self._require_current_dependencies(connection, project_id, before)
                    completion = checked.get('completion_evidence')
                    if not isinstance(completion, dict):
                        _invalid('Acceptance requires complete attested evidence')
                    if context.get('publication_required') is False:
                        if completion.get('kind') != 'without_publication':
                            _invalid('No-publication acceptance requires its explicit evidence kind')
                        from .completion import completion_without_publication
                        change = completion_without_publication(before, completion, principal.principal_id, context)
                    else:
                        from .completion import completion_change
                        change = completion_change(before, completion, principal.principal_id)
                    # Preserve the original journal before-state; add acceptance
                    # evidence to the stored token only after kernel validation.
                    prepared = json.loads(json.dumps(before))
                    prepared['metadata']['_skybuild_completion'] = change['metadata']['_skybuild_completion']
                else:
                    prepared = before
                if event == 'freeze' and context.get('publication_required') is False:
                    from .completion import freeze_without_publication_policy
                    prepared = freeze_without_publication_policy(before, context)
                after = self._apply_workflow_event(connection, principal, prepared, request, context,
                                                   journal_before=before,
                                                   receipt=evidence if event in {'integration_progress', 'exclude_from_bundle'} else None)
                if event == 'accept':
                    self._invalidate_dependents(connection, principal, project_id, task_id)
                return self._workflow_view(connection, principal, after)
            return self._idempotent(connection, principal, project_id, 'verified.workflow.' + event,
                                    idempotency_key, {'task_id': task_id, 'event': request, 'evidence': evidence}, mutation)

    def _cpu_task_binding(self, task, claim, readiness_generation, attempt_id):
        """Separate immutable attempt inputs from the current task CAS revision."""
        if not self._petri(task):
            return task['status'] == 'ready' and claim['task_revision'] == task['revision']
        from .workflow import Place
        token = self.workflow_token(task)
        binding = task['metadata']['_skybuild_workflow']['petri'].get('attempt_binding', {})
        return (token.place == Place.WORKING and token.pending_action is None
                and token.attempt_id == attempt_id and token.claim_fence == claim['fence']
                and token.input_generation == readiness_generation
                and binding == {'task_revision': claim['task_revision'], 'input_generation': token.input_generation,
                                'attempt_id': token.attempt_id, 'claim_fence': claim['fence']})

    def get_task(self, principal, project_id, task_id) -> dict:
        with self._connection() as connection:
            self._authorize(connection, principal, project_id, 'tasks:read')
            return self._task(connection, project_id, task_id)

    def update_task(self, principal, project_id, task_id, body: dict, expected_revision: int, idempotency_key: str) -> dict:
        _body(body, TASK_FIELDS)
        if set(body) & {'status', 'phase', 'blocker'}:
            _invalid('Workflow state changes require a guarded task action')
        if isinstance(body.get('metadata'), dict) and set(body['metadata']) & {'_skybuild_workflow', '_skybuild_completion'}:
            _invalid('Workflow metadata is managed by task actions')
        def changes(before, connection):
            if '_skybuild_workflow' not in before['metadata']:
                return body
            updated = dict(body)
            workflow = json.loads(json.dumps(before['metadata']['_skybuild_workflow']))
            workflow['generation'] = workflow.get('generation', 0) + 1
            workflow['last_action'] = 'definition_edit'
            workflow['reason'] = 'Task fields edited'
            workflow.pop('deferral', None)
            updated['metadata'] = {**body.get('metadata', before['metadata']), '_skybuild_workflow': workflow}
            updated.update({'status': 'blocked', 'phase': 'reassess', 'blocker': 'Task definition changed',
                            'next_action': 'Reassess changed task definition'})
            return updated
        return self._change_task(principal, project_id, task_id, expected_revision, idempotency_key,
                                 'task.update', {'task_id': task_id, 'revision': expected_revision, 'body': body},
                                 changes)

    def task_action(self, principal, project_id, task_id, action: str, body: dict, expected_revision: int, idempotency_key: str) -> dict:
        from .workflow import ACTIONS, ACTION_FIELDS, action_change

        if action not in ACTIONS:
            _invalid('Unsupported task action')
        _body(body, ACTION_FIELDS)
        def changes(before, connection):
            if self._petri(before):
                return self._petri_manual_change(connection, principal, project_id, before, action, body)
            if action == 'ready':
                if not before['acceptance_criteria'] or self._has_started_history(connection, project_id, task_id):
                    raise DomainError('workflow_conflict', 'Readiness requires acceptance and no started history', 409)
                self._require_current_dependencies(connection, project_id, before)
            result = action_change(before, action, body)
            milestone = body.get('milestone_task_id')
            if milestone is not None:
                milestone_task = self._task(connection, project_id, milestone)
                if milestone_task['status'] == 'done':
                    raise DomainError('workflow_conflict', 'Deferral milestone is already complete', 409)
            return result
        return self._change_task(principal, project_id, task_id, expected_revision, idempotency_key,
                                 'task.action.' + action,
                                 {'task_id': task_id, 'revision': expected_revision, 'body': body}, changes,
                                 reason=body.get('reason'))

    def _petri_manual_change(self, connection, principal, project_id, before, action, body):
        """Bridge existing owner controls without letting legacy actions bypass Petri."""
        from dataclasses import replace
        from .workflow import Place, TaskWorkflow, action_change
        current = self._principal(connection, principal.principal_id)
        if not current.is_admin:
            raise DomainError('authorization', 'Only owner/admin may change workflow controls', 403)
        self._require_no_effect_exposure(connection, project_id, before['task_id'])
        token = self.workflow_token(before)
        if token.superseded or token.pending_action:
            raise DomainError('workflow_conflict', 'Resolve the pending or retired task before reassessment', 409)
        if action == 'ready':
            if (token.place not in {Place.HOLD, Place.READY} or not before['acceptance_criteria'] or
                    self._has_started_history(connection, project_id, before['task_id'])):
                raise DomainError('workflow_conflict', 'Readiness requires acceptance and no started history', 409)
        elif action == 'rework':
            if token.place in {Place.HOLD, Place.DEFERRED}:
                raise DomainError('workflow_conflict', 'Release or resume the task before rework', 409)
        action_before = {**before, 'status': 'blocked'} if action == 'reassess' and token.place == Place.DEFERRED else before
        result = action_change(action_before, action, body)
        # A deferral is a control change, never execution or spending approval.
        if action == 'defer':
            context = self._workflow_context(connection, current, before)
            context['current_inputs'] = True
            event = {'event': 'defer', 'operation_id': 'manual-defer', 'expected_revision': token.revision, **body}
            token = TaskWorkflow().apply(token, event, context)
        elif action == 'resume':
            if token.place != Place.DEFERRED:
                raise DomainError('workflow_conflict', 'Only a deferred task can resume', 409)
        if action == 'defer':
            result['metadata']['_skybuild_workflow']['petri']['token'] = token.to_dict()
        return result

    def _require_structural_change(self, connection, project_id, task):
        """Permit resolved Petri scope changes while retaining legacy history guards."""
        if self._petri(task):
            from .workflow import Place
            token = self.workflow_token(task)
            if token.place == Place.DONE or token.superseded or token.pending_action is not None:
                raise DomainError('workflow_conflict', 'Reopen or resolve the task before structural changes', 409)
            self._require_no_effect_exposure(connection, project_id, task['task_id'])
        elif task['status'] != 'proposed' or self._has_started_history(connection, project_id, task['task_id']):
            raise DomainError('workflow_conflict', 'Structural changes require a proposed task with no execution history', 409)

    def _require_current_dependencies(self, connection, project_id, task):
        """The caller holds the graph lock throughout evaluation and publication."""
        from .completion import current_completion

        for dependency_id in task['dependencies']:
            dependency = self._task(connection, project_id, dependency_id)
            readiness = connection.execute(
                'SELECT input_generation, assessed_generation FROM task_readiness '
                'WHERE project_id = %s AND task_id = %s', (project_id, dependency_id)).fetchone()
            if (not current_completion(dependency) or not readiness or
                    readiness['input_generation'] != readiness['assessed_generation']):
                raise DomainError('workflow_conflict', f'Dependency {dependency_id} lacks current completion', 409)

    def complete_task(self, principal, project_id, task_id, body: dict, expected_revision: int, idempotency_key: str) -> dict:
        """Record owner-attested code acceptance; never contact or publish to GitHub."""
        from .completion import completion_change

        _body(body, {'reason', 'generation', 'source_head', 'author', 'policy_ref',
                     'acceptance', 'checks', 'review', 'publication'})
        def changes(before, connection):
            if self._petri(before):
                raise DomainError('workflow_conflict', 'Petri completion requires verified workflow acceptance', 409)
            current = self._principal(connection, principal.principal_id)
            if not current.is_admin:
                raise DomainError('authorization', 'Only an owner/admin may attest completion', 403)
            self._require_current_dependencies(connection, project_id, before)
            return completion_change(before, body, current.principal_id)
        return self._change_task(principal, project_id, task_id, expected_revision, idempotency_key,
                                 'task.action.completed',
                                 {'task_id': task_id, 'revision': expected_revision, 'body': body}, changes,
                                 reason=body.get('reason'))

    def reconcile_due_deferrals(self, principal, project_id, idempotency_key: str, *, limit=100, after_task_id=None, now=None) -> dict:
        """Perform bounded CPU-only catch-up with graph-locked milestone proof."""
        from .workflow import due_deferral
        self._page(limit, 0)
        if after_task_id is not None:
            _identifier(after_task_id, 'after_task_id')
        _text(idempotency_key, 'idempotency_key')
        now = now or datetime.now(timezone.utc)
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            _invalid('Reconciliation time must include a timezone')
        tasks = self.list_tasks(principal, project_id, limit=limit, after_task_id=after_task_id, by_id=True)
        changed = []
        for snapshot in tasks:
            if snapshot['status'] != 'deferred':
                continue
            key = hashlib.sha256(f'{idempotency_key}:{snapshot["task_id"]}:{snapshot["revision"]}'.encode()).hexdigest()
            with self._connection() as connection:
                actor = self._authorize(connection, principal, project_id, 'tasks:write')
                def mutation():
                    self._graph_lock(connection, project_id)
                    task = self._task(connection, project_id, snapshot['task_id'], lock=True)
                    if task['revision'] != snapshot['revision'] or task['status'] != 'deferred':
                        return False
                    milestone = task['metadata'].get('_skybuild_workflow', {}).get('deferral', {}).get('milestone_task_id')
                    milestone_done = False
                    if milestone:
                        try:
                            self._require_current_dependencies(connection, project_id, {'dependencies': [milestone]})
                        except DomainError as error:
                            if error.code not in {'workflow_conflict', 'not_found'}:
                                raise
                        else:
                            milestone_done = True
                    if not due_deferral(task, now, milestone_done):
                        return False
                    # A missed trigger resumes to Ready for reassessment, never starts work.
                    self._require_no_effect_exposure(connection, project_id, task['task_id'])
                    if self._petri(task):
                        token = self.workflow_token(task)
                        if token.superseded or token.pending_action:
                            return False
                    from .workflow import action_change
                    values = action_change(task, 'resume', {'reason': 'Deferral trigger reached',
                                                           'next_action': 'Reassess current definition and dependencies'})
                    self._replace_task(connection, actor, project_id, task['task_id'], task, values,
                                       operation='resume', reason='Deferral trigger reached')
                    return True
                try:
                    resumed = self._idempotent(connection, actor, project_id, 'task.resume_due', key,
                                               {'task_id': snapshot['task_id'], 'revision': snapshot['revision'],
                                                'trigger_check': True}, mutation)
                except DomainError as error:
                    if error.code not in {'claim_conflict', 'capacity_conflict', 'effect_conflict'}:
                        raise
                    # Existing ownership remains visible. The next sweep can retry.
                    continue
                if resumed:
                    changed.append(snapshot['task_id'])
        return {'scanned': len(tasks), 'reassessed': changed,
                'next_after_task_id': tasks[-1]['task_id'] if len(tasks) == limit else None}

    def _change_task(self, principal, project_id, task_id, expected_revision, idempotency_key,
                     operation, payload, changes, *, reason=None):
        _identifier(task_id, 'task_id')
        if type(expected_revision) is not int or expected_revision < 1:
            _invalid('Updates require a positive expected revision')
        if operation == 'task.update' and not payload['body']:
            _invalid('Updates require changes')
        with self._connection() as connection:
            principal = self._authorize(connection, principal, project_id, 'tasks:write')
            def mutation():
                self._graph_lock(connection, project_id)
                before = self._task(connection, project_id, task_id, lock=True)
                if before['revision'] != expected_revision:
                    raise DomainError('stale_revision', 'Task revision has changed', 409)
                if before['status'] == 'superseded':
                    raise DomainError('workflow_conflict', 'Superseded task cannot be edited or resumed', 409)
                body = changes(before, connection)
                return self._replace_task(connection, principal, project_id, task_id, before, body,
                                          operation=operation.removeprefix('task.action.') if operation.startswith('task.action.') else 'updated',
                                          reason=reason or 'Task updated')
            return self._idempotent(connection, principal, project_id, operation, idempotency_key, payload, mutation)

    def split_task(self, principal, project_id, task_id, children: list[dict], incoming: dict[str, list[str]],
                   reason: str, expected_revision: int, idempotency_key: str) -> dict:
        """Replace one inactive task with explicit children and incoming edge mapping."""
        _identifier(task_id, 'task_id')
        reason = _text(reason, 'reason', 4096)
        if type(expected_revision) is not int or expected_revision < 1:
            _invalid('Split requires a positive expected revision')
        if not isinstance(children, list) or not 2 <= len(children) <= 10 or not isinstance(incoming, dict) or len(incoming) > 100:
            _invalid('Split requires 2–10 children and an incoming dependency map')
        allowed = {'task_id', 'title', 'description', 'acceptance_criteria', 'architecture_refs',
                   'dependencies', 'responsible', 'next_action'}
        child_ids = []
        for child in children:
            _body(child, allowed)
            if not {'task_id', 'title', 'description', 'acceptance_criteria', 'dependencies'} <= set(child):
                _invalid('Each child needs identity, scope, acceptance and dependency allocation')
            child_ids.append(_identifier(child['task_id'], 'child task_id'))
            if not child['acceptance_criteria']:
                _invalid('Each child needs acceptance criteria')
            self._task_values({key: value for key, value in child.items() if key != 'task_id'})
            if task_id in child['dependencies']:
                _invalid('A child cannot depend on its superseded source')
        if len(set(child_ids)) != len(child_ids) or task_id in child_ids:
            _invalid('Child IDs must be new and distinct')
        for dependent, replacement_ids in incoming.items():
            _identifier(dependent, 'dependent task_id')
            if (not isinstance(replacement_ids, list) or not replacement_ids
                    or any(not isinstance(item, str) for item in replacement_ids)
                    or len(replacement_ids) != len(set(replacement_ids))
                    or any(item not in child_ids for item in replacement_ids)):
                _invalid('Incoming dependencies need explicit child IDs')
        payload = {'task_id': task_id, 'children': children, 'incoming': incoming,
                   'reason': reason, 'revision': expected_revision}
        _body(payload, set(payload))
        with self._connection() as connection:
            principal = self._authorize(connection, principal, project_id, 'tasks:write')
            def mutation():
                self._graph_lock(connection, project_id)
                source = self._task(connection, project_id, task_id, lock=True)
                if source['revision'] != expected_revision:
                    raise DomainError('stale_revision', 'Task revision has changed', 409)
                self._require_structural_change(connection, project_id, source)
                dependents = connection.execute('SELECT task_id FROM task_dependencies WHERE project_id = %s AND dependency_id = %s ORDER BY task_id', (project_id, task_id)).fetchall()
                if set(incoming) != {row['task_id'] for row in dependents}:
                    raise DomainError('workflow_conflict', 'Incoming dependency mapping is incomplete or stale', 409)
                for dependent in incoming:
                    state = self._task(connection, project_id, dependent, lock=True)
                    self._require_structural_change(connection, project_id, state)
                for child_id in child_ids:
                    if connection.execute('SELECT 1 FROM tasks WHERE project_id = %s AND task_id = %s', (project_id, child_id)).fetchone():
                        raise DomainError('conflict', 'Child task ID already exists', 409)
                allocated_dependencies = {dependency for child in children for dependency in child['dependencies']}
                if not set(source['dependencies']) <= allocated_dependencies:
                    _invalid('Split cannot drop source prerequisites')
                allocated_acceptance = {criterion for child in children for criterion in child['acceptance_criteria']}
                if not set(source['acceptance_criteria']) <= allocated_acceptance:
                    _invalid('Split cannot drop source acceptance criteria')
                allocated_refs = {reference for child in children for reference in child.get('architecture_refs', [])}
                if not set(source['architecture_refs']) <= allocated_refs:
                    _invalid('Split cannot drop source architecture references')
                created = []
                for child in children:
                    child_id = child['task_id']
                    values = self._task_values({key: value for key, value in child.items() if key != 'task_id'})
                    values['priority'] = source['priority']
                    values['metadata'] = {'_skybuild_workflow': {'generation': 0, 'split_from': task_id}}
                    if self._petri(source):
                        from .reconciliation import replacement_token
                        replacement_token(project_id, child_id, values)
                    columns = [key for key in values if key != 'dependencies']
                    parameters = [Jsonb(values[key]) if key == 'metadata' else values[key] for key in columns]
                    connection.execute(sql.SQL('INSERT INTO tasks (project_id, task_id, {}) VALUES (%s, %s, {})').format(
                        sql.SQL(', ').join(map(sql.Identifier, columns)), sql.SQL(', ').join(sql.Placeholder() for _ in columns)),
                        (project_id, child_id, *parameters))
                for child in children:
                    self._dependencies(connection, project_id, child['task_id'], child['dependencies'])
                    after = self._task(connection, project_id, child['task_id'])
                    self._journal(connection, principal, after, operation='created', reason=f'Split from {task_id}: {reason}')
                    connection.execute('INSERT INTO task_lineage (event_id, project_id, source_task_id, target_task_id, action) VALUES (%s, %s, %s, %s, %s)',
                                       (uuid4(), project_id, task_id, child['task_id'], 'split'))
                    created.append(after)
                rewired = []
                for dependent, replacement_ids in incoming.items():
                    before = self._task(connection, project_id, dependent, lock=True)
                    metadata = dict(before['metadata'])
                    workflow = dict(metadata.get('_skybuild_workflow', {}))
                    workflow['generation'] = workflow.get('generation', 0) + 1
                    workflow.update({'last_action': 'dependency_rewired', 'reason': f'Split of {task_id}'})
                    workflow.pop('deferral', None)
                    metadata['_skybuild_workflow'] = workflow
                    dependencies = sorted((set(before['dependencies']) - {task_id}) | set(replacement_ids))
                    rewired.append(self._replace_task(connection, principal, project_id, dependent, before,
                        {'dependencies': dependencies, 'status': 'blocked', 'phase': 'reassess',
                         'blocker': f'Dependency {task_id} was split', 'next_action': 'Reassess replacement dependencies',
                         'metadata': metadata}, operation='dependency_rewired', reason=f'Split of {task_id}: {reason}'))
                metadata = dict(source['metadata'])
                workflow = dict(metadata.get('_skybuild_workflow', {}))
                workflow['generation'] = workflow.get('generation', 0) + 1
                workflow.update({'last_action': 'split', 'reason': reason, 'replaced_by': child_ids})
                workflow.pop('deferral', None)
                metadata['_skybuild_workflow'] = workflow
                superseded = self._replace_task(connection, principal, project_id, task_id, source,
                    {'status': 'superseded', 'phase': 'superseded', 'blocker': 'Replaced by split tasks',
                     'next_action': 'Review linked replacement tasks', 'metadata': metadata},
                    operation='split', reason=reason)
                # Cascading invalidation can change an earlier returned row later in this transaction.
                return {'source': self._task(connection, project_id, task_id),
                        'children': [self._task(connection, project_id, row['task_id']) for row in created],
                        'rewired': [self._task(connection, project_id, row['task_id']) for row in rewired]}
            return self._idempotent(connection, principal, project_id, 'task.split', idempotency_key, payload, mutation)

    def task_lineage(self, principal, project_id, task_id) -> list[dict]:
        _identifier(task_id, 'task_id')
        with self._connection() as connection:
            self._authorize(connection, principal, project_id, 'tasks:read')
            self._task(connection, project_id, task_id)
            return _public(connection.execute(
                'SELECT * FROM task_lineage WHERE project_id = %s AND (source_task_id = %s OR target_task_id = %s) '
                'ORDER BY created_at, event_id', (project_id, task_id, task_id),
            ).fetchall())

    def merge_tasks(self, principal, project_id, source_task_ids: list[str], target: dict,
                    incoming_dependents: list[str], expected_revisions: dict[str, int], reason: str,
                    idempotency_key: str) -> dict:
        """Merge never-started proposed tasks into one new task in a transaction."""
        reason = _text(reason, 'reason', 4096)
        if (not isinstance(source_task_ids, list) or not 2 <= len(source_task_ids) <= 10
                or any(not isinstance(item, str) for item in source_task_ids)
                or len(set(source_task_ids)) != len(source_task_ids)):
            _invalid('Merge requires 2–10 distinct source IDs')
        for task_id in source_task_ids:
            _identifier(task_id, 'source task_id')
        if not isinstance(expected_revisions, dict) or set(expected_revisions) != set(source_task_ids):
            _invalid('Expected revisions must cover every source')
        if any(type(revision) is not int or revision < 1 for revision in expected_revisions.values()):
            _invalid('Expected revisions must be positive integers')
        if (not isinstance(incoming_dependents, list) or len(incoming_dependents) > 100
                or any(not isinstance(item, str) for item in incoming_dependents)
                or len(set(incoming_dependents)) != len(incoming_dependents)):
            _invalid('Incoming dependents must be distinct task IDs')
        for dependent in incoming_dependents:
            _identifier(dependent, 'dependent task_id')
        allowed = {'task_id', 'title', 'description', 'acceptance_criteria', 'architecture_refs',
                   'dependencies', 'responsible', 'next_action'}
        _body(target, allowed)
        if not {'task_id', 'title', 'description', 'acceptance_criteria', 'dependencies'} <= set(target):
            _invalid('Merged target needs identity, scope, acceptance and dependencies')
        target_id = _identifier(target['task_id'], 'target task_id')
        if target_id in source_task_ids or not target['acceptance_criteria']:
            _invalid('Merged target must be new and have acceptance criteria')
        self._task_values({key: value for key, value in target.items() if key != 'task_id'})
        if set(target['dependencies']) & set(source_task_ids):
            _invalid('Merged target cannot depend on replaced sources')
        payload = {'source_task_ids': source_task_ids, 'target': target, 'incoming_dependents': incoming_dependents,
                   'expected_revisions': expected_revisions, 'reason': reason}
        _body(payload, set(payload))
        with self._connection() as connection:
            principal = self._authorize(connection, principal, project_id, 'tasks:write')
            def mutation():
                self._graph_lock(connection, project_id)
                sources = [self._task(connection, project_id, task_id, lock=True) for task_id in sorted(source_task_ids)]
                for source in sources:
                    if source['revision'] != expected_revisions[source['task_id']]:
                        raise DomainError('stale_revision', 'Source task revision has changed', 409)
                    self._require_structural_change(connection, project_id, source)
                rows = connection.execute(
                    'SELECT DISTINCT task_id FROM task_dependencies WHERE project_id = %s AND dependency_id = ANY(%s) '
                    'AND NOT (task_id = ANY(%s)) ORDER BY task_id',
                    (project_id, source_task_ids, source_task_ids),
                ).fetchall()
                if set(incoming_dependents) != {row['task_id'] for row in rows}:
                    raise DomainError('workflow_conflict', 'Incoming dependency list is incomplete or stale', 409)
                for dependent in incoming_dependents:
                    state = self._task(connection, project_id, dependent, lock=True)
                    self._require_structural_change(connection, project_id, state)
                if connection.execute('SELECT 1 FROM tasks WHERE project_id = %s AND task_id = %s', (project_id, target_id)).fetchone():
                    raise DomainError('conflict', 'Merged target task ID already exists', 409)
                required_dependencies = set().union(*(set(source['dependencies']) for source in sources)) - set(source_task_ids)
                required_acceptance = set().union(*(set(source['acceptance_criteria']) for source in sources))
                required_refs = set().union(*(set(source['architecture_refs']) for source in sources))
                if (not required_dependencies <= set(target['dependencies']) or not required_acceptance <= set(target['acceptance_criteria'])
                        or not required_refs <= set(target.get('architecture_refs', []))):
                    _invalid('Merge cannot drop source prerequisites, acceptance criteria or architecture references')
                values = self._task_values({key: value for key, value in target.items() if key != 'task_id'})
                values['priority'] = min(source['priority'] for source in sources)
                values['metadata'] = {'_skybuild_workflow': {'generation': 0, 'merged_from': sorted(source_task_ids)}}
                if any(self._petri(source) for source in sources):
                    from .reconciliation import replacement_token
                    replacement_token(project_id, target_id, values)
                columns = [key for key in values if key != 'dependencies']
                parameters = [Jsonb(values[key]) if key == 'metadata' else values[key] for key in columns]
                connection.execute(sql.SQL('INSERT INTO tasks (project_id, task_id, {}) VALUES (%s, %s, {})').format(
                    sql.SQL(', ').join(map(sql.Identifier, columns)), sql.SQL(', ').join(sql.Placeholder() for _ in columns)),
                    (project_id, target_id, *parameters))
                self._dependencies(connection, project_id, target_id, target['dependencies'])
                merged = self._task(connection, project_id, target_id)
                self._journal(connection, principal, merged, operation='created', reason=f'Merged from {", ".join(sorted(source_task_ids))}: {reason}')
                rewired = []
                for dependent in incoming_dependents:
                    before = self._task(connection, project_id, dependent, lock=True)
                    metadata = dict(before['metadata'])
                    workflow = dict(metadata.get('_skybuild_workflow', {}))
                    workflow['generation'] = workflow.get('generation', 0) + 1
                    workflow.update({'last_action': 'dependency_rewired', 'reason': 'Source tasks merged'})
                    metadata['_skybuild_workflow'] = workflow
                    dependencies = sorted((set(before['dependencies']) - set(source_task_ids)) | {target_id})
                    rewired.append(self._replace_task(connection, principal, project_id, dependent, before,
                        {'dependencies': dependencies, 'status': 'blocked', 'phase': 'reassess',
                         'blocker': 'Dependencies merged', 'next_action': 'Reassess merged dependency',
                         'metadata': metadata}, operation='dependency_rewired', reason=f'Merge into {target_id}: {reason}'))
                replaced = []
                for source in sources:
                    # Earlier source retirement can invalidate another source in this atomic merge.
                    source = self._task(connection, project_id, source['task_id'], lock=True)
                    metadata = dict(source['metadata'])
                    workflow = dict(metadata.get('_skybuild_workflow', {}))
                    workflow['generation'] = workflow.get('generation', 0) + 1
                    workflow.update({'last_action': 'merge', 'reason': reason, 'replaced_by': [target_id]})
                    metadata['_skybuild_workflow'] = workflow
                    replaced.append(self._replace_task(connection, principal, project_id, source['task_id'], source,
                        {'status': 'superseded', 'phase': 'superseded', 'blocker': 'Replaced by merged task',
                         'next_action': 'Review linked merged task', 'metadata': metadata},
                        operation='merge', reason=reason))
                    connection.execute('INSERT INTO task_lineage (event_id, project_id, source_task_id, target_task_id, action) VALUES (%s, %s, %s, %s, %s)',
                                       (uuid4(), project_id, source['task_id'], target_id, 'merge'))
                # Persist the final committed projection in the idempotency receipt.
                return {'sources': [self._task(connection, project_id, row['task_id']) for row in replaced],
                        'target': self._task(connection, project_id, target_id),
                        'rewired': [self._task(connection, project_id, row['task_id']) for row in rewired]}
            return self._idempotent(connection, principal, project_id, 'task.merge', idempotency_key, payload, mutation)

    @staticmethod
    def _has_started_history(connection, project_id, task_id):
        return bool(connection.execute(
            "SELECT 1 FROM task_journal WHERE project_id = %s AND task_id = %s AND ("
            "before_state->>'status' IN ('in-progress', 'done') OR after_state->>'status' IN ('in-progress', 'done') "
            "OR before_state->>'phase' IN ('working', 'validating', 'integrating') OR after_state->>'phase' IN ('working', 'validating', 'integrating') "
            "OR before_state #>> '{metadata,_skybuild_workflow,petri,token,place}' IN ('working','validating','integrating','done') "
            "OR after_state #>> '{metadata,_skybuild_workflow,petri,token,place}' IN ('working','validating','integrating','done')) LIMIT 1",
            (project_id, task_id),
        ).fetchone())

    @staticmethod
    def _require_no_effect_exposure(connection, project_id, task_id):
        if connection.execute("SELECT 1 FROM cpu_reservations WHERE project_id = %s AND task_id = %s AND state = 'reserved' LIMIT 1",
                              (project_id, task_id)).fetchone():
            raise DomainError('capacity_conflict', 'Task has a held CPU reservation', 409)
        if connection.execute('SELECT 1 FROM task_claims WHERE project_id = %s AND task_id = %s AND held',
                              (project_id, task_id)).fetchone():
            raise DomainError('claim_conflict', 'Task ownership must be reconciled before mutation', 409)
        if connection.execute(
            'SELECT 1 FROM task_effects WHERE project_id = %s AND task_id = %s '
            'AND exposure_held LIMIT 1', (project_id, task_id)).fetchone():
            raise DomainError('effect_conflict', 'Task has unresolved effect exposure', 409)

    def create_effect_intent(self, principal, project_id, task_id, body, expected_revision, idempotency_key, *, claim_fence=None):
        """Persist intent only. Caller-supplied references never grant launch authority."""
        fields = {'operation_id', 'attempt_id', 'authority_epoch', 'authority_generation',
                  'input_digest', 'policy_digest', 'allocation_refs'}
        _body(body, fields)
        if set(body) != fields:
            _invalid('Effect intent requires all identity and allocation fields')
        for field in ('operation_id', 'attempt_id'):
            _identifier(body[field], field)
        for field in ('authority_epoch', 'authority_generation'):
            if type(body[field]) is not int or not 1 <= body[field] < 2**63:
                _invalid(f'{field} must be a positive 64-bit integer')
        for field in ('input_digest', 'policy_digest'):
            value = body[field]
            if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
                _invalid(f'{field} must be a SHA-256 hex digest')
        refs = body['allocation_refs']
        if not isinstance(refs, list) or not 1 <= len(refs) <= 100 or len(set(map(str, refs))) != len(refs):
            _invalid('allocation_refs requires 1–100 distinct references')
        for ref in refs:
            _identifier(ref, 'allocation reference')
        if type(expected_revision) is not int or not 1 <= expected_revision < 2**63:
            _invalid('Effect intent requires a positive expected revision')
        payload = {'task_id': task_id, 'revision': expected_revision, 'body': body}
        if claim_fence is not None:
            payload['claim_fence'] = claim_fence
        digest = hashlib.sha256(_json({'project_id': project_id, **payload}).encode()).hexdigest()
        with self._connection() as connection:
            principal = self._authorize_effect_writer(connection, principal, project_id)
            def mutation():
                self._graph_lock(connection, project_id)
                # Global operation identity must serialize even across projects and actors.
                connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))',
                                   ('skybuild:effect:' + body['operation_id'],))
                prior = connection.execute('SELECT * FROM task_effects WHERE operation_id = %s',
                                           (body['operation_id'],)).fetchone()
                if prior:
                    if prior['intent_hash'] != digest:
                        raise DomainError('idempotency_conflict', 'Operation ID has different intent', 409)
                    return _public(prior)
                task = self._task(connection, project_id, task_id, lock=True)
                self._require_claim_fence(connection, principal, project_id, task_id, claim_fence)
                if task['revision'] != expected_revision:
                    raise DomainError('stale_revision', 'Task revision has changed', 409)
                if task['status'] in {'superseded', 'done', 'deferred'}:
                    raise DomainError('workflow_conflict', 'Task cannot register effect intent in this state', 409)
                connection.execute(
                    'INSERT INTO task_effects (operation_id, project_id, task_id, attempt_id, task_revision, '
                    'authority_epoch, authority_generation, input_digest, policy_digest, allocation_refs, intent_hash) '
                    'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)',
                    (body['operation_id'], project_id, task_id, body['attempt_id'], expected_revision,
                     body['authority_epoch'], body['authority_generation'], body['input_digest'],
                     body['policy_digest'], refs, digest))
                after = _public(connection.execute('SELECT * FROM task_effects WHERE operation_id = %s',
                                                  (body['operation_id'],)).fetchone())
                self._effect_journal(connection, principal, after, None, 'Intent registered; no dispatch authorized')
                return after
            return self._idempotent(connection, principal, project_id, 'effect.intent', idempotency_key, payload, mutation)

    def observe_effect(self, principal, project_id, operation_id, state, reason, idempotency_key, *, claim_fence=None):
        """Hold uncertainty durably or cancel an intent never exposed to external I/O.

        Unknown is deliberately irreversible here. No adapter proof format is qualified.
        This method neither dispatches nor authorizes an external operation.
        """
        _identifier(operation_id, 'operation_id')
        if state not in ('unknown', 'cancelled'):
            _invalid('Only unknown exposure or never-dispatched cancellation is supported')
        reason = _text(reason, 'reason', 4096)
        payload = {'operation_id': operation_id, 'state': state, 'reason': reason}
        if claim_fence is not None:
            payload['claim_fence'] = claim_fence
        with self._connection() as connection:
            principal = self._authorize_effect_writer(connection, principal, project_id)
            def mutation():
                self._graph_lock(connection, project_id)
                before = connection.execute('SELECT * FROM task_effects WHERE operation_id = %s AND project_id = %s FOR UPDATE',
                                            (operation_id, project_id)).fetchone()
                if not before:
                    raise DomainError('not_found', 'Effect operation not found', 404)
                self._require_claim_fence(connection, principal, project_id, before['task_id'], claim_fence)
                if before['state'] == state:
                    return _public(before)
                if before['state'] != 'intent':
                    raise DomainError('effect_conflict', 'Unknown exposure cannot be cancelled or released', 409)
                connection.execute('UPDATE task_effects SET state = %s, exposure_held = %s WHERE operation_id = %s',
                                   (state, state != 'cancelled', operation_id))
                after = _public(connection.execute('SELECT * FROM task_effects WHERE operation_id = %s',
                                                  (operation_id,)).fetchone())
                self._effect_journal(connection, principal, after, _public(before), reason)
                return after
            return self._idempotent(connection, principal, project_id, 'effect.observe', idempotency_key, payload, mutation)

    def effect_history(self, principal, project_id, operation_id, *, limit=100, offset=0):
        self._page(limit, offset)
        _identifier(operation_id, 'operation_id')
        with self._connection() as connection:
            self._authorize(connection, principal, project_id, 'tasks:read')
            if not connection.execute('SELECT 1 FROM task_effects WHERE operation_id = %s AND project_id = %s',
                                      (operation_id, project_id)).fetchone():
                raise DomainError('not_found', 'Effect operation not found', 404)
            return _public(connection.execute('SELECT * FROM effect_journal WHERE operation_id = %s '
                                             'ORDER BY created_at, event_id LIMIT %s OFFSET %s',
                                             (operation_id, limit, offset)).fetchall())

    def _authorize_effect_writer(self, connection, principal, project_id):
        current = self._authorize(connection, principal, project_id, 'tasks:write')
        if not current.is_admin:
            raise DomainError('authorization', 'Only an owner/admin may record effect observations', 403)
        return current

    @staticmethod
    def _effect_journal(connection, principal, after, before, reason):
        connection.execute('INSERT INTO effect_journal (event_id, operation_id, actor, action, reason, before_state, '
                           'after_state) VALUES (%s, %s, %s, %s, %s, %s, %s)',
                           (uuid4(), after['operation_id'], principal.principal_id, after['state'], reason,
                            Jsonb(before) if before else None, Jsonb(after)))

    def task_history(self, principal, project_id, task_id, *, limit=100, offset=0) -> list[dict]:
        self._page(limit, offset)
        with self._connection() as connection:
            self._authorize(connection, principal, project_id, 'tasks:read')
            self._task(connection, project_id, task_id)
            return _public(connection.execute('SELECT * FROM task_journal WHERE project_id = %s AND task_id = %s ORDER BY revision LIMIT %s OFFSET %s', (project_id, task_id, limit, offset)).fetchall())

    def _recipient(self, connection, project_id, recipient):
        _identifier(recipient, 'recipient')
        row = connection.execute('SELECT is_admin FROM lock_principal(%s)', (recipient,)).fetchone()
        grant = connection.execute("SELECT 1 FROM principal_grants WHERE principal_id = %s AND project_id = %s AND operation = 'cord:read'", (recipient, project_id)).fetchone()
        if not row or not (row['is_admin'] or grant):
            _invalid('Recipient must have Cord read access to this project')

    @staticmethod
    def _message(connection, project_id, message_id, *, lock=False):
        try:
            parsed = UUID(str(message_id))
        except (ValueError, TypeError, AttributeError):
            _invalid('message_id must be a UUID')
        row = connection.execute('SELECT * FROM messages WHERE project_id = %s AND message_id = %s' + (' FOR UPDATE' if lock else ''), (project_id, parsed)).fetchone()
        if not row:
            raise DomainError('not_found', 'Message not found', 404)
        return _public(row)

    @staticmethod
    def _cord_journal(connection, principal, action, after, before=None, reply=None):
        connection.execute(
            'INSERT INTO cord_journal (event_id, project_id, message_id, actor, action, original_message_id, '
            'reply_message_id, before_state, after_state, reply_state) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)',
            (uuid4(), after['project_id'], UUID(after['message_id']), principal.principal_id, action,
             UUID(after['message_id']) if action == 'reply' else None,
             UUID(reply['message_id']) if reply else None, Jsonb(before) if before else None,
             Jsonb(after), Jsonb(reply) if reply else None),
        )

    def _message_transition(self, connection, principal, original, action):
        column = 'delivered_at' if action == 'receipt' else 'handled_at'
        connection.execute(sql.SQL('UPDATE messages SET {0} = COALESCE({0}, now()) WHERE project_id = %s AND message_id = %s').format(sql.Identifier(column)), (original['project_id'], UUID(original['message_id'])))
        after = self._message(connection, original['project_id'], original['message_id'])
        self._cord_journal(connection, principal, action, after, original)
        return after

    def _insert_message(self, connection, principal, project_id, body):
        self._recipient(connection, project_id, body.get('recipient'))
        subject = _text(body.get('subject'), 'subject', 500)
        text = _text(body.get('body'), 'body', 32768)
        category = _text(body.get('category', 'misc'), 'category', 100)
        urgency = _text(body.get('urgency', 'normal'), 'urgency', 100)
        task_id, reply_to, expires = body.get('task_id'), body.get('reply_to'), body.get('expires_at')
        if task_id is not None:
            self._task(connection, project_id, task_id)
        if reply_to is not None:
            self._authorize(connection, principal, project_id, 'cord:handle')
            original = self._message(connection, project_id, reply_to, lock=True)
            if not principal.is_admin and original['recipient'] != principal.principal_id:
                raise DomainError('authorization', 'Only the recipient may reply to a message', 403)
            if body['recipient'] != original['sender']:
                _invalid('A reply must be addressed to the original sender')
        if expires is not None:
            try:
                expires = datetime.fromisoformat(_text(expires, 'expires_at', 100).replace('Z', '+00:00'))
                if expires.tzinfo is None or expires <= datetime.now(timezone.utc):
                    _invalid('expires_at must be a future timestamp with timezone')
            except ValueError:
                _invalid('expires_at must be a future timestamp with timezone')
        message_id = uuid4()
        connection.execute('INSERT INTO messages (message_id, project_id, sender, recipient, subject, body, category, urgency, task_id, reply_to, expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)', (message_id, project_id, principal.principal_id, body['recipient'], subject, text, category, urgency, task_id, UUID(str(reply_to)) if reply_to else None, expires))
        result = self._message(connection, project_id, message_id)
        if reply_to is not None:
            connection.execute('UPDATE messages SET replied_at = COALESCE(replied_at, now()) WHERE project_id = %s AND message_id = %s', (project_id, UUID(str(reply_to))))
            self._cord_journal(connection, principal, 'reply', self._message(connection, project_id, reply_to), original, result)
        else:
            self._cord_journal(connection, principal, 'send', result)
        return result

    def send_message(self, principal, project_id, body: dict, idempotency_key: str) -> dict:
        _body(body, {'recipient', 'subject', 'body', 'category', 'urgency', 'task_id', 'reply_to', 'expires_at'})
        with self._connection() as connection:
            principal = self._authorize(connection, principal, project_id, 'cord:send')
            return self._idempotent(connection, principal, project_id, 'cord.send', idempotency_key, body, lambda: self._insert_message(connection, principal, project_id, body))

    def inbox(self, principal, project_id, *, limit=100, offset=0) -> list[dict]:
        self._page(limit, offset)
        with self._connection() as connection:
            principal = self._authorize(connection, principal, project_id, 'cord:read')
            return _public(connection.execute('SELECT * FROM messages WHERE project_id = %s AND recipient = %s AND handled_at IS NULL AND (expires_at IS NULL OR expires_at > now()) ORDER BY accepted_at, message_id LIMIT %s OFFSET %s', (project_id, principal.principal_id, limit, offset)).fetchall())

    def message_action(self, principal, project_id, message_id, action: str, body: dict, idempotency_key: str) -> dict:
        if not isinstance(action, str) or action not in {'receipt', 'handle', 'reply'}:
            _invalid('Unknown message action')
        _body(body, {'subject', 'body', 'handle_original'} if action == 'reply' else set())
        if 'handle_original' in body and not isinstance(body['handle_original'], bool):
            _invalid('handle_original must be boolean')
        with self._connection() as connection:
            principal = self._authorize(connection, principal, project_id, 'cord:read' if action == 'receipt' else 'cord:handle')
            if action == 'reply':
                self._authorize(connection, principal, project_id, 'cord:send')
            def mutation():
                original = self._message(connection, project_id, message_id, lock=True)
                if original['recipient'] != principal.principal_id and not principal.is_admin:
                    raise DomainError('authorization', 'Only the recipient may act on a message', 403)
                if action == 'reply':
                    reply = self._insert_message(connection, principal, project_id, {
                        'recipient': original['sender'], 'subject': body.get('subject'), 'body': body.get('body'),
                        'reply_to': original['message_id'], 'task_id': original['task_id'],
                    })
                    if body.get('handle_original', False):
                        self._message_transition(connection, principal, self._message(connection, project_id, message_id), 'handle')
                    return reply
                return self._message_transition(connection, principal, original, action)
            return self._idempotent(connection, principal, project_id, 'cord.' + action, idempotency_key, {'message_id': str(message_id), 'body': body}, mutation)
