"""Launch-free effect registry against task-owned disposable PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import psycopg
import pytest

from skybuild.contracts import DomainError
from test_store import actors, create, store


def intent(store, owner, project, task, operation_id=None, key=None):
    body = {'operation_id': operation_id or uuid4().hex, 'attempt_id': 'attempt-1',
            'authority_epoch': 1, 'authority_generation': 1, 'input_digest': 'a' * 64,
            'policy_digest': 'b' * 64, 'allocation_refs': ['allocation-1']}
    return store.create_effect_intent(owner, project, task['task_id'], body, task['revision'], key or uuid4().hex)


def test_stable_operation_duplicate_and_conflicting_identity(store, actors):
    project, people = actors
    owner = people['owner']
    task = create(store, owner, project)
    operation = uuid4().hex
    original = intent(store, owner, project, task, operation, 'first')
    assert intent(store, owner, project, task, operation, 'first') == original
    assert intent(store, owner, project, task, operation, 'second') == original
    other = create(store, owner, project, 'other')
    with pytest.raises(DomainError, match='different intent'):
        intent(store, owner, project, other, operation)
    assert len(store.effect_history(owner, project, operation)) == 1


def test_unknown_retained_and_cannot_release_through_task_edit(store, actors):
    project, people = actors
    owner = people['owner']
    task = create(store, owner, project)
    effect = intent(store, owner, project, task)
    unknown = store.observe_effect(owner, project, effect['operation_id'], 'unknown', 'Lost acknowledgement', 'unknown')
    assert unknown['exposure_held']
    assert store.observe_effect(owner, project, effect['operation_id'], 'unknown', 'Lost acknowledgement', 'unknown') == unknown
    with pytest.raises(DomainError, match='cannot be cancelled'):
        store.observe_effect(owner, project, effect['operation_id'], 'cancelled', 'Lease expired', 'cancel')
    with pytest.raises(DomainError, match='unresolved effect'):
        store.update_task(owner, project, task['task_id'], {'description': 'Changed'}, 1, 'edit')
    assert store.get_task(owner, project, task['task_id']) == task
    assert len(store.task_history(owner, project, task['task_id'])) == 1
    assert [e['action'] for e in store.effect_history(owner, project, effect['operation_id'])] == ['intent', 'unknown']


def test_intent_cancel_unlocks_edit_but_retry_never_recreates_effect(store, actors):
    project, people = actors
    owner = people['owner']
    task = create(store, owner, project)
    operation = uuid4().hex
    first = intent(store, owner, project, task, operation, 'intent')
    with pytest.raises(DomainError, match='unresolved effect'):
        store.update_task(owner, project, task['task_id'], {'description': 'Changed'}, 1, 'edit')
    cancelled = store.observe_effect(owner, project, operation, 'cancelled', 'Never dispatched', 'cancel')
    assert not cancelled['exposure_held']
    changed = store.update_task(owner, project, task['task_id'], {'description': 'Changed'}, 1, 'edit')
    assert changed['revision'] == 2
    assert intent(store, owner, project, task, operation, 'intent') == first  # historical receipt only
    assert intent(store, owner, project, task, operation, 'new-key') == cancelled
    with pytest.raises(DomainError):
        store.observe_effect(owner, project, operation, 'unknown', 'Late dispatch forbidden', 'late')


def test_dependent_exposure_rolls_back_upstream_edit(store, actors):
    project, people = actors
    owner = people['owner']
    parent = create(store, owner, project, 'parent')
    dependent = create(store, owner, project, 'dependent', dependencies=['parent'])
    intent(store, owner, project, dependent)
    with pytest.raises(DomainError, match='unresolved effect'):
        store.update_task(owner, project, 'parent', {'description': 'Changed'}, 1, 'edit')
    assert store.get_task(owner, project, 'parent') == parent
    assert store.get_task(owner, project, 'dependent') == dependent


def test_cancel_racing_unknown_has_single_serialized_outcome(store, actors):
    project, people = actors
    owner = people['owner']
    task = create(store, owner, project)
    effect = intent(store, owner, project, task)
    def observe(state):
        try:
            return store.observe_effect(owner, project, effect['operation_id'], state, state, state)
        except DomainError as error:
            return error
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(observe, ['unknown', 'cancelled']))
    assert sum(isinstance(result, DomainError) for result in results) == 1
    history = store.effect_history(owner, project, effect['operation_id'])
    assert len(history) == 2
    assert history[-1]['after_state']['exposure_held'] == (history[-1]['action'] == 'unknown')


def test_effect_and_journal_immutability(store, actors):
    project, people = actors
    task = create(store, people['owner'], project)
    effect = intent(store, people['owner'], project, task)
    for statement in ('UPDATE effect_journal SET reason = reason', 'DELETE FROM effect_journal',
                      'TRUNCATE effect_journal', 'DELETE FROM task_effects',
                      "UPDATE task_effects SET task_revision = task_revision + 1"):
        with pytest.raises(DomainError) as caught:
            with store._connection() as connection:
                connection.execute(statement)
        assert isinstance(caught.value.__cause__, psycopg.Error)
    assert len(store.effect_history(people['owner'], project, effect['operation_id'])) == 1


def test_effect_writes_require_admin_and_markdown_authority_guard(store, actors):
    project, people = actors
    task = create(store, people['owner'], project)
    with pytest.raises(DomainError) as caught:
        intent(store, people['worker'], project, task)
    assert caught.value.code == 'authorization'
    with store._connection() as connection:
        connection.execute("INSERT INTO ledger_imports (project_id, commit_id, content_sha256, import_sha256, task_count, status_counts, authority) "
                           "VALUES (%s, 'commit', %s, %s, 1, '{}'::jsonb, 'markdown')", (project, 'c' * 64, 'd' * 64))
    with pytest.raises(DomainError) as caught:
        intent(store, people['owner'], project, task)
    assert caught.value.code == 'authority'


def test_edit_waiting_on_intent_commit_cannot_publish(store, actors, monkeypatch):
    project, people = actors
    owner = people['owner']
    task = create(store, owner, project)
    registered, release = Event(), Event()
    original = store._effect_journal
    def pause(connection, principal, after, before, reason):
        original(connection, principal, after, before, reason)
        if after['project_id'] == project and after['state'] == 'intent':
            registered.set()
            assert release.wait(3)
    monkeypatch.setattr(store, '_effect_journal', pause)
    with ThreadPoolExecutor(max_workers=2) as pool:
        registering = pool.submit(intent, store, owner, project, task)
        assert registered.wait(3)
        editing = pool.submit(store.update_task, owner, project, task['task_id'],
                              {'description': 'Racing edit'}, 1, 'edit')
        release.set()
        registering.result(timeout=5)
        with pytest.raises(DomainError, match='unresolved effect'):
            editing.result(timeout=5)
    assert store.get_task(owner, project, task['task_id']) == task


def test_edit_committed_before_intent_rejects_stale_definition(store, actors):
    project, people = actors
    owner = people['owner']
    original = create(store, owner, project)
    changed = store.update_task(owner, project, original['task_id'], {'description': 'New definition'}, 1, 'edit')
    with pytest.raises(DomainError) as caught:
        intent(store, owner, project, original)
    assert caught.value.code == 'stale_revision'
    assert intent(store, owner, project, changed)['task_revision'] == changed['revision']


@pytest.mark.parametrize('effect_on', ['source', 'dependent'])
@pytest.mark.parametrize('action', ['split', 'merge'])
def test_structural_guards_cover_sources_and_rewired_dependents(store, actors, effect_on, action):
    project, people = actors
    owner = people['owner']
    a = create(store, owner, project, 'a', acceptance_criteria=['A'])
    b = create(store, owner, project, 'b', acceptance_criteria=['B'])
    dependent = create(store, owner, project, 'dependent', dependencies=['a'])
    effect = intent(store, owner, project, a if effect_on == 'source' else dependent)
    store.observe_effect(owner, project, effect['operation_id'], 'unknown', 'Uncertain start', 'unknown')
    with pytest.raises(DomainError, match='unresolved effect'):
        if action == 'split':
            children = [{'task_id': name, 'title': name, 'description': name, 'acceptance_criteria': ['A'],
                         'architecture_refs': [], 'dependencies': []} for name in ['c', 'd']]
            store.split_task(owner, project, 'a', children, {'dependent': ['c', 'd']}, 'Separate', 1, 'split')
        else:
            store.merge_tasks(owner, project, ['a', 'b'],
                {'task_id': 'c', 'title': 'C', 'description': 'A and B', 'acceptance_criteria': ['A', 'B'],
                 'architecture_refs': [], 'dependencies': []}, ['dependent'], {'a': 1, 'b': 1}, 'Combine', 'merge')
    assert store.get_task(owner, project, 'a') == a
    assert store.get_task(owner, project, 'b') == b
    assert store.get_task(owner, project, 'dependent') == dependent
    assert store.task_lineage(owner, project, 'a') == []
    with pytest.raises(DomainError) as caught:
        store.get_task(owner, project, 'c')
    assert caught.value.code == 'not_found'
