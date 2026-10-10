"""PostgreSQL behavior tests. Set SKYBUILD_TEST_DSN to an owned disposable database."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import secrets
from threading import Barrier
from uuid import uuid4
from unittest.mock import patch

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import pytest

from skybuild.contracts import DomainError, Principal
from skybuild.store import OPERATIONS, Store


TEST_WORKER_OPERATIONS = OPERATIONS - {'tasks:usage-record', 'tasks:usage-resolve'}


@pytest.fixture(scope='module')
def store():
    dsn = os.environ.get('SKYBUILD_TEST_DSN')
    if not dsn:
        pytest.skip('SKYBUILD_TEST_DSN must select a task-owned disposable PostgreSQL database')
    expected = conninfo_to_dict(dsn).get('dbname')
    if not expected or not expected.startswith('skybuild_test'):
        pytest.fail('Store tests require an explicitly named skybuild_test disposable database')
    result = Store(dsn, expected)
    result.migrate()
    return result


@pytest.fixture
def actors(store):
    project = 'test-' + uuid4().hex
    seed_api_authority(store, project)
    identities = {}
    for name in ('owner', 'worker', 'peer', 'outsider'):
        principal_id, token = name + '-' + uuid4().hex, secrets.token_urlsafe(32)
        grants = {project: TEST_WORKER_OPERATIONS} if name != 'outsider' else {}
        store.provision_principal(principal_id, token, is_admin=name == 'owner', grants=grants)
        identities[name] = store.authenticate(token)
        identities[name + '_token'] = token
    return project, identities


def seed_api_authority(store, project):
    """Test projects start API-owned only when the fixture records that explicitly."""
    with store._connection() as connection:
        connection.execute(
            "INSERT INTO ledger_imports (project_id, commit_id, content_sha256, import_sha256, "
            "task_count, status_counts, authority) VALUES (%s, 'test-api', %s, %s, 0, '{}'::jsonb, 'api')",
            (project, '0' * 64, '1' * 64),
        )


def legacy_create(store, principal, project, task_id='T1', **fields):
    """Build a synthetic pre-Petri task for existing compatibility tests.

    Replace only the pure enrollment helper during fixture construction. The
    transaction, journal, authorization and all subsequent guards are unchanged.
    Default-enrollment acceptance tests call Store.create_task directly.
    """
    with patch.object(store, '_new_task_metadata', side_effect=lambda task: task['metadata']):
        return store.create_task(principal, project, {'task_id': task_id, 'title': 'Test task', 'description': 'Full brief', **fields}, 'create-' + task_id)


# Existing legacy scenarios retain their fixture import without changing scope.
create = legacy_create


def test_manual_workflow_actions_are_journaled_and_idempotent(store, actors):
    project, identities = actors
    task = create(store, identities['owner'], project, 'workflow')
    milestone = create(store, identities['owner'], project, 'milestone')
    request = {'reason': 'Wait for milestone', 'milestone_task_id': milestone['task_id'],
               'next_action': 'Reassess after milestone completion'}
    deferred = store.task_action(identities['owner'], project, 'workflow', 'defer', request, 1, 'defer-key')
    assert deferred['status'] == 'deferred'
    assert deferred['phase'] == 'deferred'
    assert deferred['metadata']['_skybuild_workflow']['deferral']['milestone_task_id'] == 'milestone'
    assert store.task_action(identities['owner'], project, 'workflow', 'defer', request, 1, 'defer-key') == deferred
    changed_deferral = store.task_action(identities['owner'], project, 'workflow', 'defer',
                                         {'reason': 'Wait longer', 'milestone_task_id': milestone['task_id']}, 2, 'defer-again')
    assert changed_deferral['metadata']['_skybuild_workflow']['interrupted_phase'] == task['phase']
    error('idempotency_conflict', lambda: store.task_action(identities['owner'], project, 'workflow', 'defer',
                                                           {'reason': 'Different', 'milestone_task_id': 'milestone'}, 1, 'defer-key'))
    error('stale_revision', lambda: store.task_action(identities['owner'], project, 'workflow', 'resume',
                                                     {'reason': 'Now ready'}, 1, 'stale'))
    resumed = store.task_action(identities['owner'], project, 'workflow', 'resume', {'reason': 'Milestone complete'}, 3, 'resume-key')
    assert (resumed['status'], resumed['phase']) == ('blocked', 'reassess')
    assert resumed['metadata']['_skybuild_workflow']['generation'] == 3
    assert 'deferral' not in resumed['metadata']['_skybuild_workflow']
    rework = store.task_action(identities['owner'], project, 'workflow', 'rework',
                               {'reason': 'Acceptance changed', 'next_action': 'Revise tests'}, 4, 'rework-key')
    assert (rework['status'], rework['phase'], rework['blocker']) == ('blocked', 'needs-rework', 'Acceptance changed')
    reassess = store.task_action(identities['owner'], project, 'workflow', 'reassess', {'reason': 'New evidence'}, 5, 'reassess-key')
    assert (reassess['status'], reassess['phase']) == ('blocked', 'reassess')
    history = store.task_history(identities['owner'], project, 'workflow')
    assert [event['operation'] for event in history] == ['created', 'defer', 'defer', 'resume', 'rework', 'reassess']
    assert [event['reason'] for event in history[1:]] == ['Wait for milestone', 'Wait longer', 'Milestone complete', 'Acceptance changed', 'New evidence']
    assert all(event['actor'] == identities['owner'].principal_id for event in history)


def test_workflow_invalid_trigger_rolls_back(store, actors):
    project, identities = actors
    task = create(store, identities['owner'], project, 'workflow-invalid')
    error('not_found', lambda: store.task_action(identities['owner'], project, task['task_id'], 'defer',
                                                 {'reason': 'Wait', 'milestone_task_id': 'missing'}, 1, 'missing'))
    error('validation', lambda: store.task_action(identities['owner'], project, task['task_id'], 'defer',
                                                  {'reason': 'Wait', 'until': '2026-10-09T12:00:00'}, 1, 'naive'))
    error('workflow_conflict', lambda: store.task_action(identities['owner'], project, task['task_id'], 'defer',
                                                         {'reason': 'Wait', 'until': '2020-01-01T12:00:00+00:00'}, 1, 'past'))
    assert store.get_task(identities['owner'], project, task['task_id'])['revision'] == 1
    assert len(store.task_history(identities['owner'], project, task['task_id'])) == 1


def test_guarded_ready_requires_independent_unstarted_definition(store, actors):
    project, people = actors
    owner = people['owner']
    create(store, owner, project, 'bare')
    error('workflow_conflict', lambda: store.task_action(owner, project, 'bare', 'ready',
                                                         {'reason': 'Definition reviewed'}, 1, 'bare-ready'))
    create(store, owner, project, 'prerequisite')
    create(store, owner, project, 'dependent-ready', dependencies=['prerequisite'], acceptance_criteria=['check'])
    error('workflow_conflict', lambda: store.task_action(owner, project, 'dependent-ready', 'ready',
                                                         {'reason': 'Definition reviewed'}, 1, 'dependent-ready'))
    task = create(store, owner, project, 'independent-ready', acceptance_criteria=['check result'])
    error('validation', lambda: store.task_action(owner, project, task['task_id'], 'ready',
                                                  {'reason': 'Definition reviewed', 'next_action': 'Start now'}, 1, 'forge-admission'))
    ready = store.task_action(owner, project, task['task_id'], 'ready', {'reason': 'Definition reviewed'}, 1, 'ready')
    assert (ready['status'], ready['phase'], ready['blocker']) == ('ready', 'ready-for-work', None)
    assert ready['next_action'] == 'Await explicit admission and ownership'
    assert store.task_action(owner, project, task['task_id'], 'ready', {'reason': 'Definition reviewed'}, 1, 'ready') == ready
    error('workflow_conflict', lambda: store.task_action(owner, project, task['task_id'], 'ready',
                                                         {'reason': 'Again'}, 2, 'ready-again'))
    changed = store.update_task(owner, project, task['task_id'], {'description': 'Revised brief'}, 2, 'revise-ready')
    assert (changed['status'], changed['phase']) == ('blocked', 'reassess')
    assert store.task_action(owner, project, task['task_id'], 'ready', {'reason': 'Re-reviewed'}, 3, 'ready-after-edit')['status'] == 'ready'
    assert [event['operation'] for event in store.task_history(owner, project, task['task_id'])] == ['created', 'ready', 'updated', 'ready']


def test_task_id_cursor_does_not_shift_when_priority_changes(store, actors):
    project, people = actors
    owner = people['owner']
    create(store, owner, project, 'cursor-a', priority=30)
    create(store, owner, project, 'cursor-b', priority=20)
    create(store, owner, project, 'cursor-c', priority=10)
    first = store.list_tasks(owner, project, limit=2, by_id=True)
    assert [task['task_id'] for task in first] == ['cursor-a', 'cursor-b']
    store.update_task(owner, project, 'cursor-c', {'priority': 0}, 1, 'change-priority')
    second = store.list_tasks(owner, project, limit=2, after_task_id=first[-1]['task_id'])
    assert [task['task_id'] for task in second] == ['cursor-c']
    error('validation', lambda: store.list_tasks(owner, project, limit=2, offset=1, by_id=True))


def test_separate_deferral_cycles_capture_current_interrupted_phase(store, actors):
    project, people = actors
    owner = people['owner']
    create(store, owner, project, 'cycles')
    first = store.task_action(owner, project, 'cycles', 'defer',
                              {'reason': 'First wait', 'until': '2030-01-01T00:00:00+00:00'}, 1, 'first')
    assert first['metadata']['_skybuild_workflow']['interrupted_phase'] == 'triage'
    store.task_action(owner, project, 'cycles', 'resume', {'reason': 'Resume'}, 2, 'resume')
    store.task_action(owner, project, 'cycles', 'rework', {'reason': 'New requirements'}, 3, 'rework')
    second = store.task_action(owner, project, 'cycles', 'defer',
                               {'reason': 'Second wait', 'until': '2030-02-01T00:00:00+00:00'}, 4, 'second')
    assert second['metadata']['_skybuild_workflow']['interrupted_phase'] == 'needs-rework'


def test_due_deferral_catch_up_and_reserved_state(store, actors):
    from datetime import datetime, timezone

    project, people = actors
    owner = people['owner']
    task = create(store, owner, project, 'due')
    error('validation', lambda: store.update_task(owner, project, 'due', {'status': 'done'}, 1, 'forge-done'))
    error('validation', lambda: store.update_task(owner, project, 'due', {'phase': 'ready-for-bundle'}, 1, 'forge-phase'))
    error('validation', lambda: store.update_task(owner, project, 'due', {'metadata': {'_skybuild_workflow': {'generation': 0}}}, 1, 'forge-generation'))
    store.task_action(owner, project, 'due', 'defer',
                      {'reason': 'Wait until date', 'until': '2030-01-01T00:00:00+00:00'}, 1, 'defer')
    assert store.reconcile_due_deferrals(owner, project, 'scan-before', now=datetime(2029, 1, 1, tzinfo=timezone.utc))['reassessed'] == []
    scan = store.reconcile_due_deferrals(owner, project, 'scan-after', now=datetime(2030, 1, 2, tzinfo=timezone.utc))
    assert scan['reassessed'] == ['due']
    current = store.get_task(owner, project, 'due')
    assert (current['status'], current['phase'], current['revision']) == ('blocked', 'reassess', 3)
    assert current['metadata']['_skybuild_workflow']['interrupted_phase'] == task['phase']
    assert store.reconcile_due_deferrals(owner, project, 'scan-retry', now=datetime(2030, 1, 2, tzinfo=timezone.utc))['reassessed'] == []
    changed = store.update_task(owner, project, 'due', {'metadata': {'note': 'kept'}}, 3, 'metadata-edit')
    assert changed['metadata']['_skybuild_workflow']['generation'] == 3
    assert (changed['status'], changed['phase']) == ('blocked', 'reassess')
    assert changed['metadata']['note'] == 'kept'
    assert [event['operation'] for event in store.task_history(owner, project, 'due')] == ['created', 'defer', 'resume', 'updated']


def test_due_reconciliation_pages_past_first_hundred(store, actors):
    from datetime import datetime, timezone

    project, people = actors
    owner = people['owner']
    for index in range(100):
        create(store, owner, project, f'filler-{index:03}')
    create(store, owner, project, 'z-due-later', priority=1)
    store.task_action(owner, project, 'z-due-later', 'defer',
                      {'reason': 'Wait until date', 'until': '2030-01-01T00:00:00+00:00'}, 1, 'defer-later')
    now = datetime(2030, 1, 2, tzinfo=timezone.utc)
    first = store.reconcile_due_deferrals(owner, project, 'page-one', now=now)
    assert first == {'scanned': 100, 'reassessed': [], 'next_after_task_id': 'filler-099'}
    store.update_task(owner, project, 'filler-000', {'priority': 1000}, 1, 'move-priority')
    second = store.reconcile_due_deferrals(owner, project, 'page-two', after_task_id=first['next_after_task_id'], now=now)
    assert second == {'scanned': 1, 'reassessed': ['z-due-later'], 'next_after_task_id': None}


def test_split_rewires_explicit_dependencies_and_keeps_lineage(store, actors):
    project, people = actors
    owner = people['owner']
    create(store, owner, project, 'base')
    source = create(store, owner, project, 'source', dependencies=['base'], acceptance_criteria=['one', 'two'],
                    architecture_refs=['architecture 5', 'plan 3a'], priority=7)
    create(store, owner, project, 'dependent', dependencies=['source'])
    children = [
        {'task_id': 'child-one', 'title': 'First scope', 'description': 'First brief',
         'acceptance_criteria': ['one'], 'architecture_refs': ['architecture 5'], 'dependencies': ['base']},
        {'task_id': 'child-two', 'title': 'Second scope', 'description': 'Second brief',
         'acceptance_criteria': ['two'], 'architecture_refs': ['plan 3a'], 'dependencies': []},
    ]
    error('validation', lambda: store.split_task(owner, project, 'source',
                                                  [{**children[0], 'architecture_refs': []}, children[1]],
                                                  {'dependent': ['child-one', 'child-two']},
                                                  'Dropped reference', 1, 'split-drop-ref'))
    result = store.split_task(owner, project, 'source', children,
                              {'dependent': ['child-one', 'child-two']}, 'Separate acceptance', 1, 'split-key')
    assert result['source']['status'] == 'superseded'
    error('workflow_conflict', lambda: create(store, owner, project, 'late-dependent', dependencies=['source']))
    create(store, owner, project, 'later-edit')
    error('workflow_conflict', lambda: store.update_task(owner, project, 'later-edit',
                                                         {'dependencies': ['source']}, 1, 'late-edge'))
    assert {child['task_id'] for child in result['children']} == {'child-one', 'child-two'}
    assert all(child['priority'] == source['priority'] for child in result['children'])
    assert {reference for child in result['children'] for reference in child['architecture_refs']} == {'architecture 5', 'plan 3a'}
    assert result['rewired'][0]['dependencies'] == ['child-one', 'child-two']
    assert result['rewired'][0]['phase'] == 'reassess'
    assert store.split_task(owner, project, 'source', children,
                            {'dependent': ['child-one', 'child-two']}, 'Separate acceptance', 1, 'split-key') == result
    assert len(store.task_lineage(owner, project, 'source')) == 2
    assert len(store.task_lineage(owner, project, 'child-one')) == 1
    assert [row['operation'] for row in store.task_history(owner, project, 'source')] == ['created', 'split']
    assert [row['operation'] for row in store.task_history(owner, project, 'dependent')] == ['created', 'dependency_rewired']
    error('workflow_conflict', lambda: store.update_task(owner, project, 'source', {'title': 'Rewrite history'}, 2, 'edit-source'))
    with store._connection() as connection:
        with pytest.raises(psycopg.Error):
            connection.execute("DELETE FROM task_lineage WHERE source_task_id = 'source'")


def test_split_refuses_incomplete_map_and_cycle_without_partial_rows(store, actors):
    project, people = actors
    owner = people['owner']
    create(store, owner, project, 'source')
    create(store, owner, project, 'dependent', dependencies=['source'])
    children = [
        {'task_id': 'first', 'title': 'First', 'description': 'First', 'acceptance_criteria': ['one'], 'dependencies': []},
        {'task_id': 'second', 'title': 'Second', 'description': 'Second', 'acceptance_criteria': ['two'], 'dependencies': []},
    ]
    error('workflow_conflict', lambda: store.split_task(owner, project, 'source', children, {}, 'Missing map', 1, 'missing-map'))
    assert {task['task_id'] for task in store.list_tasks(owner, project)} == {'source', 'dependent'}
    cyclic = [{**children[0], 'dependencies': ['dependent']}, children[1]]
    error('dependency_cycle', lambda: store.split_task(owner, project, 'source', cyclic,
                                                       {'dependent': ['first']}, 'Cycle', 1, 'cycle'))
    assert {task['task_id'] for task in store.list_tasks(owner, project)} == {'source', 'dependent'}
    assert len(store.task_history(owner, project, 'source')) == 1
    error('validation', lambda: store.split_task(owner, project, 'source',
                                                  [{**children[0], 'dependencies': 123}, children[1]],
                                                  {'dependent': ['first']}, 'Invalid dependencies', 1, 'invalid-list'))
    error('validation', lambda: store.split_task(owner, project, 'source', children,
                                                  {'dependent': [['first']]}, 'Invalid mapping', 1, 'invalid-map'))


def test_split_refuses_reworked_started_task(store, actors):
    project, people = actors
    owner = people['owner']
    create(store, owner, project, 'started')
    with store._connection() as connection:
        connection.execute("UPDATE tasks SET status = 'in-progress', phase = 'working' WHERE project_id = %s AND task_id = 'started'", (project,))
    store.task_action(owner, project, 'started', 'rework', {'reason': 'Interrupted work'}, 1, 'rework-started')
    children = [
        {'task_id': 'a', 'title': 'A', 'description': 'A', 'acceptance_criteria': ['a'], 'dependencies': []},
        {'task_id': 'b', 'title': 'B', 'description': 'B', 'acceptance_criteria': ['b'], 'dependencies': []},
    ]
    error('workflow_conflict', lambda: store.split_task(owner, project, 'started', children, {}, 'Split', 2, 'unsafe-split'))
    assert len(store.task_lineage(owner, project, 'started')) == 0


def test_split_refuses_blocked_dependent_until_effect_reconciliation_exists(store, actors):
    project, people = actors
    owner = people['owner']
    create(store, owner, project, 'source')
    create(store, owner, project, 'dependent', dependencies=['source'])
    store.task_action(owner, project, 'dependent', 'reassess', {'reason': 'Unknown prior work'}, 1, 'block-dependent')
    children = [
        {'task_id': 'a', 'title': 'A', 'description': 'A', 'acceptance_criteria': ['a'], 'dependencies': []},
        {'task_id': 'b', 'title': 'B', 'description': 'B', 'acceptance_criteria': ['b'], 'dependencies': []},
    ]
    error('workflow_conflict', lambda: store.split_task(owner, project, 'source', children,
                                                        {'dependent': ['a']}, 'Split', 1, 'blocked-dependent'))
    assert len(store.task_lineage(owner, project, 'source')) == 0


def test_merge_preserves_prerequisites_acceptance_and_history(store, actors):
    project, people = actors
    owner = people['owner']
    create(store, owner, project, 'base')
    create(store, owner, project, 'left', dependencies=['base'], acceptance_criteria=['left done'],
           architecture_refs=['architecture 5'], priority=5)
    create(store, owner, project, 'right', dependencies=['left'], acceptance_criteria=['right done'],
           architecture_refs=['plan 3a'], priority=2)
    create(store, owner, project, 'dependent', dependencies=['left', 'right'])
    target = {'task_id': 'merged', 'title': 'Combined scope', 'description': 'Full combined brief',
              'acceptance_criteria': ['left done', 'right done'],
              'architecture_refs': ['architecture 5', 'plan 3a'], 'dependencies': ['base']}
    error('validation', lambda: store.merge_tasks(owner, project, ['left', 'right'],
                                                  {**target, 'architecture_refs': ['architecture 5']},
                                                  ['dependent'], {'left': 1, 'right': 1},
                                                  'Dropped reference', 'merge-drop-ref'))
    args = (owner, project, ['left', 'right'], target, ['dependent'], {'left': 1, 'right': 1}, 'Combine related work', 'merge-key')
    result = store.merge_tasks(*args)
    assert result['target']['dependencies'] == ['base']
    assert result['target']['priority'] == 2
    assert result['target']['architecture_refs'] == ['architecture 5', 'plan 3a']
    assert result['rewired'][0]['dependencies'] == ['merged']
    assert {row['status'] for row in result['sources']} == {'superseded'}
    error('workflow_conflict', lambda: create(store, owner, project, 'merge-late-dependent', dependencies=['left']))
    assert store.merge_tasks(*args) == result
    error('idempotency_conflict', lambda: store.merge_tasks(owner, project, ['left', 'right'], target,
                                                            ['dependent'], {'left': 1, 'right': 1}, 'Changed reason', 'merge-key'))
    assert len(store.task_lineage(owner, project, 'merged')) == 2
    assert [event['operation'] for event in store.task_history(owner, project, 'left')] == ['created', 'merge']
    assert [event['operation'] for event in store.task_history(owner, project, 'dependent')] == ['created', 'dependency_rewired']


def test_merge_rejects_incomplete_map_cycle_and_dropped_acceptance(store, actors):
    project, people = actors
    owner = people['owner']
    create(store, owner, project, 'left', acceptance_criteria=['left done'])
    create(store, owner, project, 'right', acceptance_criteria=['right done'])
    create(store, owner, project, 'dependent', dependencies=['left'])
    target = {'task_id': 'merged', 'title': 'Merged', 'description': 'Merged',
              'acceptance_criteria': ['left done', 'right done'], 'dependencies': []}
    def merge(changes, incoming, key):
        return store.merge_tasks(owner, project, ['left', 'right'], changes, incoming,
                                 {'left': 1, 'right': 1}, 'Merge', key)
    error('workflow_conflict', lambda: merge(target, [], 'missing'))
    error('validation', lambda: merge({**target, 'acceptance_criteria': ['left done']}, ['dependent'], 'lost-criterion'))
    error('dependency_cycle', lambda: merge({**target, 'dependencies': ['dependent']}, ['dependent'], 'cycle'))
    assert {task['task_id'] for task in store.list_tasks(owner, project)} == {'left', 'right', 'dependent'}
    assert not store.task_lineage(owner, project, 'left')


def test_merge_refuses_stale_source_and_dropped_prerequisite(store, actors):
    project, people = actors
    owner = people['owner']
    create(store, owner, project, 'base')
    create(store, owner, project, 'left', dependencies=['base'], acceptance_criteria=['left'])
    create(store, owner, project, 'right', acceptance_criteria=['right'])
    target = {'task_id': 'merged', 'title': 'Merged', 'description': 'Merged',
              'acceptance_criteria': ['left', 'right'], 'dependencies': []}
    error('validation', lambda: store.merge_tasks(owner, project, ['left', 'right'], target, [],
                                                  {'left': 1, 'right': 1}, 'Lost prerequisite', 'lost-base'))
    store.update_task(owner, project, 'right', {'title': 'Changed right'}, 1, 'edit-right')
    error('stale_revision', lambda: store.merge_tasks(owner, project, ['left', 'right'],
                                                       {**target, 'dependencies': ['base']}, [],
                                                       {'left': 1, 'right': 1}, 'Stale', 'stale'))
    error('validation', lambda: store.merge_tasks(owner, project, [['left'], 'right'],
                                                  {**target, 'dependencies': ['base']}, [],
                                                  {'left': 1, 'right': 1}, 'Invalid IDs', 'invalid-ids'))
    assert {task['task_id'] for task in store.list_tasks(owner, project)} == {'base', 'left', 'right'}


def error(code, call):
    with pytest.raises(DomainError) as caught:
        call()
    assert caught.value.code == code
    return caught.value


def test_identity_guard_and_readiness(store):
    wrong = Store(store.dsn, 'skybuild_wrong_database')
    assert error('database_identity', wrong.migrate).status_code == 503
    error('database_identity', wrong.readiness)
    store.migrate()
    assert store.readiness() == {'ready': True, 'schema_version': 16}


def test_store_can_bind_operations_to_postgres_system_identifier(store):
    with psycopg.connect(store.dsn) as connection:
        system_identifier = str(connection.execute(
            'SELECT system_identifier::text FROM pg_control_system()').fetchone()[0])
    bound = Store(store.dsn, store.expected_database, system_identifier)
    assert bound.readiness()['ready'] is True
    wrong_cluster = Store(store.dsn, store.expected_database, str(int(system_identifier) + 1))
    error('database_identity', wrong_cluster.readiness)


def test_upgrade_001_to_002_preserves_existing_records_and_is_repeatable(store):
    database = 'skybuild_test_upgrade_' + uuid4().hex
    with psycopg.connect(store.dsn, autocommit=True) as connection:
        connection.execute(psycopg.sql.SQL('CREATE DATABASE {}').format(psycopg.sql.Identifier(database)))
    try:
        upgraded = Store(make_conninfo(store.dsn, dbname=database), database)
        source = (Path(__file__).parents[1] / 'src/skybuild/migrations/001_bootstrap.sql').read_text()
        message_id = uuid4()
        with upgraded._connection() as connection:
            connection.execute('CREATE SCHEMA skybuild')
            connection.execute('CREATE TABLE schema_migrations (version integer PRIMARY KEY, digest text NOT NULL)')
            connection.execute(source)
            connection.execute('INSERT INTO schema_migrations VALUES (1, %s)', (hashlib.sha256(source.encode()).hexdigest(),))
            connection.execute("INSERT INTO principals VALUES ('legacy-owner', 'legacy-verifier', true)")
            connection.execute("INSERT INTO tasks (project_id, task_id, title, description, status, phase, responsible, next_action) VALUES ('upgrade', 'T1', 'Existing task', 'Existing brief', 'proposed', 'triage', 'owner', 'Review')")
            connection.execute("INSERT INTO messages (message_id, project_id, sender, recipient, subject, body, category, urgency) VALUES (%s, 'upgrade', 'legacy-owner', 'legacy-owner', 'Existing message', 'Content', 'misc', 'normal')", (message_id,))
            task_before = connection.execute('SELECT * FROM tasks').fetchone()
            message_before = connection.execute('SELECT * FROM messages').fetchone()
        error('schema_mismatch', upgraded.readiness)
        upgraded.migrate()
        upgraded.migrate()
        assert upgraded.readiness() == {'ready': True, 'schema_version': 16}
        with upgraded._connection() as connection:
            assert connection.execute('SELECT * FROM tasks').fetchone() == task_before
            assert connection.execute('SELECT * FROM messages').fetchone() == message_before
            assert connection.execute('SELECT count(*) AS count FROM cord_journal').fetchone()['count'] == 0
    finally:
        with psycopg.connect(store.dsn, autocommit=True) as connection:
            connection.execute(psycopg.sql.SQL('DROP DATABASE {} WITH (FORCE)').format(psycopg.sql.Identifier(database)))


def test_credential_replacement_preserves_identity_history_and_verifier(store, actors):
    project, people = actors
    worker = people['worker']
    task = create(store, worker, project)
    replacement = secrets.token_urlsafe(32)
    store.provision_principal(worker.principal_id, replacement, grants={project: worker.grants[project]})
    error('authentication', lambda: store.authenticate(people['worker_token']))
    current = store.authenticate(replacement)
    assert current == worker
    assert store.get_task(current, project, 'T1') == task
    assert store.task_history(current, project, 'T1')[0]['actor'] == worker.principal_id
    with psycopg.connect(store.dsn) as connection:
        verifier = connection.execute('SELECT token_verifier FROM skybuild.principals WHERE principal_id = %s', (worker.principal_id,)).fetchone()[0]
    assert verifier == hashlib.sha256(replacement.encode()).hexdigest()
    assert verifier != replacement
    error('validation', lambda: store.provision_principal('short', 'weak'))


def test_project_and_operation_authorization_reload_database_grants(store, actors):
    project, people = actors
    worker = people['worker']
    create(store, worker, project)
    error('authorization', lambda: store.get_task(worker, project + '-other', 'T1'))
    error('authorization', lambda: store.list_tasks(people['outsider'], project))
    spoofed = Principal(people['outsider'].principal_id, True, {project: OPERATIONS})
    error('authorization', lambda: store.list_tasks(spoofed, project))
    store.provision_principal(worker.principal_id, people['worker_token'], grants={project: {'tasks:read'}})
    error('authorization', lambda: store.update_task(worker, project, 'T1', {'title': 'Denied'}, 1, 'denied'))
    assert store.get_task(worker, project, 'T1')['title'] == 'Test task'
    assert store.list_tasks(people['owner'], project)


def test_revision_history_restart_and_idempotency(store, actors):
    project, people = actors
    worker = people['worker']
    body = {'task_id': 'T1', 'title': 'Unicode: café λ', 'description': 'Complete description', 'metadata': {'resume': 'handoff'}}
    # This test covers pre-Petri worker actions and their original history.
    # Replace only enrollment while constructing the synthetic legacy task.
    with patch.object(store, '_new_task_metadata', side_effect=lambda task: task['metadata']):
        first = store.create_task(worker, project, body, 'same-key')
        assert store.create_task(worker, project, dict(reversed(list(body.items()))), 'same-key') == first
        error('idempotency_conflict', lambda: store.create_task(worker, project, {**body, 'title': 'Different'}, 'same-key'))
    changed = store.task_action(worker, project, 'T1', 'reassess', {'reason': 'Needs decision'}, 1, 'change')
    assert changed['revision'] == 2
    assert store.task_action(worker, project, 'T1', 'reassess', {'reason': 'Needs decision'}, 1, 'change') == changed
    error('stale_revision', lambda: store.update_task(worker, project, 'T1', {'title': 'Stale'}, 1, 'stale'))
    error('idempotency_conflict', lambda: store.task_action(worker, project, 'T1', 'reassess', {'reason': 'Different'}, 1, 'change'))
    restarted = Store(store.dsn, store.expected_database)
    assert restarted.get_task(worker, project, 'T1') == changed
    history = restarted.task_history(worker, project, 'T1')
    assert [event['revision'] for event in history] == [1, 2]
    assert history[0]['before_state'] is None
    assert history[1]['before_state'] == first
    assert history[1]['after_state'] == changed
    assert {event['actor'] for event in history} == {worker.principal_id}


def test_idempotency_scope_includes_actor_project_and_operation(store, actors):
    project, people = actors
    body = {'task_id': 'T1', 'title': 'One', 'description': 'Brief'}
    first = store.create_task(people['worker'], project, body, 'shared')
    second = store.create_task(people['peer'], project, {**body, 'task_id': 'T2'}, 'shared')
    seed_api_authority(store, project + '-other')
    third = store.create_task(people['owner'], project + '-other', body, 'shared')
    updated = store.update_task(people['worker'], project, 'T1', {'title': 'Changed'}, 1, 'shared')
    assert first['task_id'] != second['task_id']
    assert third['project_id'] != first['project_id']
    assert updated['revision'] == 2


def test_dependency_validation_rolls_back_task_and_history(store, actors):
    project, people = actors
    worker = people['worker']
    create(store, worker, project, 'A')
    create(store, worker, project, 'B', dependencies=['A'])
    error('dependency_cycle', lambda: store.update_task(worker, project, 'A', {'title': 'Rollback', 'dependencies': ['B']}, 1, 'cycle'))
    assert store.get_task(worker, project, 'A')['title'] == 'Test task'
    assert len(store.task_history(worker, project, 'A')) == 1
    error('validation', lambda: create(store, worker, project, 'C', dependencies=['missing']))
    error('not_found', lambda: store.get_task(worker, project, 'C'))
    error('validation', lambda: store.update_task(worker, project, 'A', {'dependencies': ['A']}, 1, 'self'))
    seed_api_authority(store, project + '-other')
    create(store, people['owner'], project + '-other', 'OnlyElsewhere')
    error('validation', lambda: store.update_task(worker, project, 'A', {'dependencies': ['OnlyElsewhere']}, 1, 'cross-project'))


def test_concurrent_dependencies_cannot_form_cycle(store, actors):
    project, people = actors
    worker = people['worker']
    create(store, worker, project, 'A')
    create(store, worker, project, 'B')
    barrier = Barrier(2)
    def attempt(task_id, dependency):
        barrier.wait(timeout=5)
        try:
            return store.update_task(worker, project, task_id, {'dependencies': [dependency]}, 1, 'race-' + task_id)
        except DomainError as failure:
            return failure.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda pair: attempt(*pair), [('A', 'B'), ('B', 'A')]))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert 'dependency_cycle' in results
    assert sorted(len(store.get_task(worker, project, task)['dependencies']) for task in ('A', 'B')) == [0, 1]


def test_concurrent_same_idempotency_key_creates_one_outcome(store, actors):
    project, people = actors
    worker = people['worker']
    barrier = Barrier(2)
    def attempt(_):
        barrier.wait(timeout=5)
        return create(store, worker, project)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(attempt, range(2))
    assert first == second
    assert len(store.task_history(worker, project, 'T1')) == 1


@pytest.mark.parametrize('statement', [
    "UPDATE skybuild.task_journal SET reason = 'rewritten' WHERE project_id = %s",
    'DELETE FROM skybuild.task_journal WHERE project_id = %s',
    'TRUNCATE skybuild.task_journal, skybuild.task_usage_events',
])
def test_journal_mutation_is_blocked_in_database(store, actors, statement):
    project, people = actors
    create(store, people['worker'], project)
    with psycopg.connect(store.dsn) as connection:
        with pytest.raises(psycopg.errors.RaiseException, match='^Task journal is append-only'):
            connection.execute(statement, (project,) if '%s' in statement else None)
        connection.rollback()
    assert len(store.task_history(people['worker'], project, 'T1')) == 1


def send(store, people, project, key='send', **fields):
    return store.send_message(people['worker'], project, {
        'recipient': people['peer'].principal_id, 'subject': 'Question', 'body': 'Message content', **fields,
    }, key)


def test_cord_durable_receipt_handle_and_recipient_isolation(store, actors):
    project, people = actors
    message = send(store, people, project)
    restarted = Store(store.dsn, store.expected_database)
    assert restarted.inbox(people['peer'], project) == [message]
    assert restarted.inbox(people['worker'], project) == []
    assert send(store, people, project) == message
    error('authorization', lambda: store.message_action(people['worker'], project, message['message_id'], 'receipt', {}, 'wrong-recipient'))
    receipt = store.message_action(people['peer'], project, message['message_id'], 'receipt', {}, 'seen')
    assert receipt['delivered_at'] and receipt['handled_at'] is None
    assert store.inbox(people['peer'], project) == [receipt]
    assert store.message_action(people['peer'], project, message['message_id'], 'receipt', {}, 'seen-again') == receipt
    handled = store.message_action(people['peer'], project, message['message_id'], 'handle', {}, 'handled')
    assert handled['handled_at']
    assert store.inbox(people['peer'], project) == []
    assert store.message_action(people['peer'], project, message['message_id'], 'receipt', {}, 'seen') == receipt
    error('not_found', lambda: store.message_action(people['owner'], project + '-other', message['message_id'], 'handle', {}, 'other-project'))


def test_cord_reply_is_atomic_explicit_and_idempotent(store, actors):
    project, people = actors
    first = send(store, people, project)
    body = {'subject': 'Answer', 'body': 'Reply content'}
    reply = store.message_action(people['peer'], project, first['message_id'], 'reply', body, 'reply')
    assert reply['reply_to'] == first['message_id']
    assert reply['sender'] == people['peer'].principal_id
    assert reply['recipient'] == people['worker'].principal_id
    assert store.inbox(people['peer'], project)[0]['handled_at'] is None
    assert store.inbox(people['peer'], project)[0]['replied_at']
    assert store.message_action(people['peer'], project, first['message_id'], 'reply', body, 'reply') == reply
    assert len(store.inbox(people['worker'], project)) == 1
    second = send(store, people, project, key='second')
    combined = store.message_action(people['peer'], project, second['message_id'], 'reply', {**body, 'handle_original': True}, 'combined')
    assert combined['reply_to'] == second['message_id']
    assert len(store.inbox(people['peer'], project)) == 1
    third = send(store, people, project, key='third')
    error('validation', lambda: store.message_action(people['peer'], project, third['message_id'], 'reply', {'subject': '', 'body': 'No', 'handle_original': True}, 'bad-reply'))
    untouched = [row for row in store.inbox(people['peer'], project) if row['message_id'] == third['message_id']][0]
    assert untouched['handled_at'] is None and untouched['replied_at'] is None


def test_cord_validates_project_references_and_scopes(store, actors):
    project, people = actors
    error('validation', lambda: send(store, people, project, recipient=people['outsider'].principal_id))
    error('validation', lambda: send(store, people, project, recipient='missing'))
    seed_api_authority(store, project + '-other')
    create(store, people['owner'], project + '-other', 'ForeignTask')
    error('not_found', lambda: send(store, people, project, task_id='ForeignTask'))
    foreign = store.send_message(people['owner'], project + '-other', {'recipient': people['owner'].principal_id, 'subject': 'Other', 'body': 'Content'}, 'foreign')
    error('not_found', lambda: send(store, people, project, reply_to=foreign['message_id']))
    original = send(store, people, project, key='original')
    peer = people['peer']
    store.provision_principal(peer.principal_id, people['peer_token'], grants={project: {'cord:read', 'cord:handle'}})
    error('authorization', lambda: store.message_action(peer, project, original['message_id'], 'reply', {'subject': 'No', 'body': 'Content'}, 'no-send'))
    assert store.inbox(peer, project)[0]['replied_at'] is None


def test_direct_send_reply_cannot_bypass_reply_authority_or_direction(store, actors):
    project, people = actors
    original = send(store, people, project)
    body = {'subject': 'Answer', 'body': 'Content', 'reply_to': original['message_id']}
    error('authorization', lambda: store.send_message(people['worker'], project, {**body, 'recipient': people['peer'].principal_id}, 'sender-cannot-reply'))
    error('validation', lambda: store.send_message(people['peer'], project, {**body, 'recipient': people['peer'].principal_id}, 'wrong-direction'))
    peer = people['peer']
    store.provision_principal(peer.principal_id, people['peer_token'], grants={project: {'cord:read', 'cord:send'}})
    error('authorization', lambda: store.send_message(peer, project, {**body, 'recipient': people['worker'].principal_id}, 'no-handle'))
    assert store.inbox(peer, project)[0]['replied_at'] is None
    store.provision_principal(peer.principal_id, people['peer_token'], grants={project: OPERATIONS})
    reply = store.send_message(peer, project, {**body, 'recipient': people['worker'].principal_id}, 'valid-direct-reply')
    assert reply['reply_to'] == original['message_id']
    assert store.inbox(peer, project)[0]['replied_at']
    assert store.inbox(peer, project)[0]['handled_at'] is None


def test_reply_rolls_back_insert_if_original_update_fails(store, actors):
    project, people = actors
    original = send(store, people, project)
    trigger = 'test_fail_' + uuid4().hex
    function = psycopg.sql.Identifier('skybuild', trigger)
    trigger_name = psycopg.sql.Identifier(trigger)
    with psycopg.connect(store.dsn) as connection:
        connection.execute(psycopg.sql.SQL(
            "CREATE FUNCTION {}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
            "IF NEW.message_id::text = TG_ARGV[0] THEN RAISE EXCEPTION 'Injected original update failure'; "
            "END IF; RETURN NEW; END; $$"
        ).format(function))
        connection.execute(psycopg.sql.SQL('CREATE TRIGGER {} BEFORE UPDATE ON skybuild.messages FOR EACH ROW EXECUTE FUNCTION {}({})').format(trigger_name, function, psycopg.sql.Literal(original['message_id'])))
    try:
        error('unavailable', lambda: store.message_action(people['peer'], project, original['message_id'], 'reply', {'subject': 'Answer', 'body': 'Content', 'handle_original': True}, 'rollback-reply'))
        assert store.inbox(people['worker'], project) == []
        assert store.inbox(people['peer'], project) == [original]
    finally:
        with psycopg.connect(store.dsn) as connection:
            connection.execute(psycopg.sql.SQL('DROP TRIGGER {} ON skybuild.messages').format(trigger_name))
            connection.execute(psycopg.sql.SQL('DROP FUNCTION {}()').format(function))
    reply = store.message_action(people['peer'], project, original['message_id'], 'reply', {'subject': 'Answer', 'body': 'Content', 'handle_original': True}, 'rollback-reply')
    assert store.inbox(people['worker'], project) == [reply]
    assert store.inbox(people['peer'], project) == []


def cord_history(store, project):
    with store._connection() as connection:
        return connection.execute('SELECT * FROM cord_journal WHERE project_id = %s', (project,)).fetchall()


def test_cord_journal_records_actors_and_distinct_reply_handling(store, actors):
    project, people = actors
    original = send(store, people, project)
    receipt = store.message_action(people['peer'], project, original['message_id'], 'receipt', {}, 'receipt')
    reply = store.message_action(people['peer'], project, original['message_id'], 'reply', {'subject': 'Answer', 'body': 'Content', 'handle_original': True}, 'reply-handle')
    events = cord_history(store, project)
    assert len(events) == 4
    by_action = {event['action']: event for event in events}
    assert by_action['send']['actor'] == people['worker'].principal_id
    assert by_action['send']['before_state'] is None
    assert by_action['send']['after_state'] == original
    assert by_action['receipt']['actor'] == people['peer'].principal_id
    assert by_action['receipt']['before_state'] == original
    assert by_action['receipt']['after_state'] == receipt
    replied, handled = by_action['reply'], by_action['handle']
    assert str(replied['original_message_id']) == original['message_id']
    assert str(replied['reply_message_id']) == reply['message_id']
    assert replied['reply_state'] == reply
    assert replied['after_state']['replied_at']
    assert replied['after_state']['handled_at'] is None
    assert handled['before_state'] == replied['after_state']
    assert handled['after_state']['handled_at']
    assert replied['actor'] == handled['actor'] == people['peer'].principal_id
    assert store.message_action(people['peer'], project, original['message_id'], 'reply', {'subject': 'Answer', 'body': 'Content', 'handle_original': True}, 'reply-handle') == reply
    assert len(cord_history(store, project)) == 4
    second = send(store, people, project, key='admin-message')
    store.message_action(people['owner'], project, second['message_id'], 'handle', {}, 'admin-handle')
    admin_event = next(event for event in cord_history(store, project) if str(event['message_id']) == second['message_id'] and event['action'] == 'handle')
    assert admin_event['actor'] == people['owner'].principal_id
    assert admin_event['actor'] != second['recipient']


@pytest.mark.parametrize('statement', [
    "UPDATE skybuild.cord_journal SET action = 'receipt' WHERE project_id = %s",
    'DELETE FROM skybuild.cord_journal WHERE project_id = %s',
    'TRUNCATE skybuild.cord_journal',
])
def test_cord_journal_mutation_is_blocked_in_database(store, actors, statement):
    project, people = actors
    send(store, people, project)
    with psycopg.connect(store.dsn) as connection:
        with pytest.raises(psycopg.errors.RaiseException, match='Cord journal is append-only'):
            connection.execute(statement, (project,) if '%s' in statement else None)
        connection.rollback()
    assert len(cord_history(store, project)) == 1


@pytest.mark.parametrize('action', ['send', 'receipt', 'handle', 'reply', 'reply-handle'])
def test_cord_event_insert_failure_rolls_back_state_and_idempotency(store, actors, action):
    project, people = actors
    original = None if action == 'send' else send(store, people, project)
    before = cord_history(store, project)
    trigger = 'test_cord_fail_' + uuid4().hex
    function, trigger_name = psycopg.sql.Identifier('skybuild', trigger), psycopg.sql.Identifier(trigger)
    fail_action = 'handle' if action == 'reply-handle' else action
    with psycopg.connect(store.dsn) as connection:
        connection.execute(psycopg.sql.SQL(
            "CREATE FUNCTION {}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
            "IF NEW.project_id = TG_ARGV[0] AND NEW.action = TG_ARGV[1] THEN RAISE EXCEPTION 'Injected Cord journal failure'; "
            "END IF; RETURN NEW; END; $$"
        ).format(function))
        connection.execute(psycopg.sql.SQL('CREATE TRIGGER {} BEFORE INSERT ON skybuild.cord_journal FOR EACH ROW EXECUTE FUNCTION {}({}, {})').format(trigger_name, function, psycopg.sql.Literal(project), psycopg.sql.Literal(fail_action)))
    def mutation():
        if action == 'send':
            return send(store, people, project, key='failure-key')
        body = {'subject': 'Answer', 'body': 'Content', 'handle_original': action == 'reply-handle'} if action.startswith('reply') else {}
        return store.message_action(people['peer'], project, original['message_id'], 'reply' if action.startswith('reply') else action, body, 'failure-key')
    try:
        error('unavailable', mutation)
        assert cord_history(store, project) == before
        assert store.inbox(people['worker'], project) == []
        assert store.inbox(people['peer'], project) == ([] if original is None else [original])
        with store._connection() as connection:
            assert connection.execute("SELECT count(*) AS count FROM idempotency WHERE project_id = %s AND idempotency_key = 'failure-key'", (project,)).fetchone()['count'] == 0
    finally:
        with psycopg.connect(store.dsn) as connection:
            connection.execute(psycopg.sql.SQL('DROP TRIGGER {} ON skybuild.cord_journal').format(trigger_name))
            connection.execute(psycopg.sql.SQL('DROP FUNCTION {}()').format(function))
    result = mutation()
    assert mutation() == result
    assert len(cord_history(store, project)) == len(before) + (2 if action == 'reply-handle' else 1)


@pytest.mark.parametrize('field,value', [('description', ''), ('description', '   '), ('body', ''), ('body', '   ')])
def test_store_rejects_empty_required_text(store, actors, field, value):
    project, people = actors
    if field == 'description':
        error('validation', lambda: create(store, people['worker'], project, description=value))
    else:
        error('validation', lambda: send(store, people, project, body=value))


def test_literal_unicode_escape_survives_create_update_metadata_keys_and_values(store, actors):
    project, people = actors
    literal = 'literal escape: ' + chr(92) + 'u0000'
    metadata = {literal: {'nested': [literal]}}
    first = create(store, people['worker'], project, description=literal, metadata=metadata)
    assert first['description'] == literal
    assert first['metadata'] == metadata
    updated = store.update_task(people['worker'], project, 'T1', {'description': literal + ' updated', 'metadata': {literal: literal}}, 1, 'literal-update')
    assert updated['description'] == literal + ' updated'
    assert updated['metadata'][literal] == literal
    assert set(updated['metadata']) == {literal, '_skybuild_workflow'}
    assert store.get_task(people['worker'], project, 'T1') == updated
    message = send(store, people, project, body=literal)
    assert message['body'] == literal


@pytest.mark.parametrize('fields', [
    {'description': 'actual\x00NUL'},
    {'metadata': {'actual\x00key': 'value'}},
    {'metadata': {'nested': [{'key': 'actual\x00value'}]}},
])
def test_actual_nul_in_fields_metadata_keys_or_values_is_rejected(store, actors, fields):
    project, people = actors
    error('validation', lambda: create(store, people['worker'], project, 'Invalid', **fields))
    first = create(store, people['worker'], project)
    error('validation', lambda: store.update_task(people['worker'], project, 'T1', fields, 1, 'actual-nul'))
    assert store.get_task(people['worker'], project, 'T1') == first
    assert len(store.task_history(people['worker'], project, 'T1')) == 1


def test_json_traversal_bounds_depth_and_cycles(store, actors):
    project, people = actors
    nested = {}
    for _ in range(101):
        nested = {'nested': nested}
    error('validation', lambda: create(store, people['worker'], project, 'Deep', metadata=nested))
    cyclic = {}
    cyclic['cycle'] = cyclic
    error('validation', lambda: create(store, people['worker'], project, 'Cycle', metadata=cyclic))


@pytest.mark.parametrize('identifier', ['.', '..', 'a/b', 'a\\b', 'a%2Fb', 'a\n', 'a\x85'])
def test_store_rejects_unsafe_route_identifiers(store, actors, identifier):
    project, people = actors
    error('validation', lambda: create(store, people['worker'], project, identifier))
    error('validation', lambda: store.list_tasks(people['worker'], identifier))
    error('validation', lambda: store.provision_principal(identifier, secrets.token_urlsafe(32)))


@pytest.mark.parametrize('body', [
    {'is_admin': True}, {'task_id': 'changed'}, {'status': []}, {'priority': True},
    {'metadata': {'bad': float('nan')}}, {'metadata': {'null': '\x00'}},
    {'next_action': '', 'blocker': None}, {'responsible': ''}, {'dependencies': 'T2'},
    {'title': '\ud800'}, {'metadata': {'too_large': 'x' * 16385}},
])
def test_task_update_validation_preserves_projection(store, actors, body):
    project, people = actors
    first = create(store, people['worker'], project)
    error('validation', lambda: store.update_task(people['worker'], project, 'T1', body, 1, 'bad'))
    assert store.get_task(people['worker'], project, 'T1') == first


def test_bounded_views_and_no_unfinished_task_without_next_action(store, actors):
    project, people = actors
    create(store, people['worker'], project, 'A', priority=2)
    create(store, people['worker'], project, 'B', priority=1)
    assert [row['task_id'] for row in store.list_tasks(people['worker'], project, limit=1)] == ['B']
    assert [row['task_id'] for row in store.list_tasks(people['worker'], project, limit=1, offset=1)] == ['A']
    for limit, offset in ((0, 0), (101, 0), (1, -1), (True, 0)):
        error('validation', lambda: store.list_tasks(people['worker'], project, limit=limit, offset=offset))
        error('validation', lambda: store.inbox(people['worker'], project, limit=limit, offset=offset))
    error('validation', lambda: create(store, people['worker'], project, 'Invalid', next_action=None, blocker=None))
    error('validation', lambda: create(store, people['worker'], project, 'Done', status='done', next_action=None))
