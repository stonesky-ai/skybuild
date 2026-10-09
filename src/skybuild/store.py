"""Dedicated PostgreSQL persistence. Imports never connect or migrate."""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .contracts import DomainError, Principal, valid_identifier


OPERATIONS = frozenset({'tasks:read', 'tasks:write', 'cord:send', 'cord:read', 'cord:handle'})
TASK_FIELDS = frozenset({
    'title', 'description', 'status', 'priority', 'dependencies', 'acceptance_criteria',
    'architecture_refs', 'assignee', 'phase', 'next_action', 'blocker', 'responsible', 'metadata',
})
STATUSES = frozenset({'proposed', 'ready', 'in-progress', 'blocked', 'deferred', 'done'})
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


def _public(value):
    if isinstance(value, dict):
        return {key: _public(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_public(item) for item in value]
    if isinstance(value, (datetime, UUID)):
        return value.isoformat() if isinstance(value, datetime) else str(value)
    return value


class Store:
    def __init__(self, dsn: str, expected_database: str):
        self.dsn = dsn
        self.expected_database = _text(expected_database, 'expected_database', 63)

    @contextmanager
    def _connection(self):
        try:
            with psycopg.connect(self.dsn, connect_timeout=5, row_factory=dict_row) as connection:
                actual = connection.execute('SELECT current_database() AS name').fetchone()['name']
                if actual != self.expected_database:
                    raise DomainError('database_identity', 'Database identity does not match configuration', 503)
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
        row = connection.execute('SELECT principal_id, is_admin FROM principals WHERE principal_id = %s FOR SHARE', (principal_id,)).fetchone()
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
        if not isinstance(values['metadata'], dict) or len(_json(values['metadata']).encode()) > 16384:
            _invalid('metadata must be an object of at most 16 KiB')
        if values['status'] != 'done' and not (str(values['next_action'] or '').strip() or str(values['blocker'] or '').strip()):
            _invalid('Unfinished tasks need a next action or blocker')
        return values

    @staticmethod
    def _dependencies(connection, project_id, task_id, dependencies):
        if task_id in dependencies:
            _invalid('A task cannot depend on itself')
        found = connection.execute('SELECT task_id FROM tasks WHERE project_id = %s AND task_id = ANY(%s)', (project_id, dependencies)).fetchall()
        if len(found) != len(dependencies):
            _invalid('Dependencies must refer to existing tasks in this project')
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
    def _journal(connection, principal, after, before=None):
        operation = 'updated' if before else 'created'
        connection.execute(
            'INSERT INTO task_journal (event_id, project_id, task_id, actor, operation, revision, reason, before_state, after_state) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)',
            (uuid4(), after['project_id'], after['task_id'], principal.principal_id, operation,
             after['revision'], 'Task ' + operation, Jsonb(before) if before else None, Jsonb(after)),
        )

    def create_task(self, principal, project_id, body: dict, idempotency_key: str) -> dict:
        _body(body, TASK_FIELDS | {'task_id'})
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
                self._journal(connection, principal, result)
                return result
            return self._idempotent(connection, principal, project_id, 'task.create', idempotency_key, body, mutation)

    def list_tasks(self, principal, project_id, *, limit=100, offset=0) -> list[dict]:
        self._page(limit, offset)
        with self._connection() as connection:
            self._authorize(connection, principal, project_id, 'tasks:read')
            return _public(connection.execute(TASK_SELECT + 'WHERE project_id = %s ORDER BY priority, task_id LIMIT %s OFFSET %s', (project_id, limit, offset)).fetchall())

    def get_task(self, principal, project_id, task_id) -> dict:
        with self._connection() as connection:
            self._authorize(connection, principal, project_id, 'tasks:read')
            return self._task(connection, project_id, task_id)

    def update_task(self, principal, project_id, task_id, body: dict, expected_revision: int, idempotency_key: str) -> dict:
        _body(body, TASK_FIELDS)
        _identifier(task_id, 'task_id')
        if type(expected_revision) is not int or expected_revision < 1 or not body:
            _invalid('Updates require changes and a positive expected revision')
        with self._connection() as connection:
            principal = self._authorize(connection, principal, project_id, 'tasks:write')
            def mutation():
                self._graph_lock(connection, project_id)
                before = self._task(connection, project_id, task_id, lock=True)
                if before['revision'] != expected_revision:
                    raise DomainError('stale_revision', 'Task revision has changed', 409)
                values = self._task_values(body, before)
                columns = [key for key in values if key != 'dependencies']
                parameters = [Jsonb(values[key]) if key == 'metadata' else values[key] for key in columns]
                assignments = sql.SQL(', ').join(sql.SQL('{} = %s').format(sql.Identifier(key)) for key in columns)
                connection.execute(sql.SQL('UPDATE tasks SET {}, revision = revision + 1, updated_at = now() WHERE project_id = %s AND task_id = %s').format(assignments), (*parameters, project_id, task_id))
                self._dependencies(connection, project_id, task_id, values['dependencies'])
                after = self._task(connection, project_id, task_id)
                self._journal(connection, principal, after, before)
                return after
            return self._idempotent(connection, principal, project_id, 'task.update', idempotency_key, {'task_id': task_id, 'revision': expected_revision, 'body': body}, mutation)

    def task_history(self, principal, project_id, task_id, *, limit=100, offset=0) -> list[dict]:
        self._page(limit, offset)
        with self._connection() as connection:
            self._authorize(connection, principal, project_id, 'tasks:read')
            self._task(connection, project_id, task_id)
            return _public(connection.execute('SELECT * FROM task_journal WHERE project_id = %s AND task_id = %s ORDER BY revision LIMIT %s OFFSET %s', (project_id, task_id, limit, offset)).fetchall())

    def _recipient(self, connection, project_id, recipient):
        _identifier(recipient, 'recipient')
        row = connection.execute('SELECT is_admin FROM principals WHERE principal_id = %s FOR SHARE', (recipient,)).fetchone()
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
