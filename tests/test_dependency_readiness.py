"""Disposable PostgreSQL regressions for atomic readiness and invalidation."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest

from skybuild.contracts import DomainError
from test_store import actors, create, store


def completed_fixture(store, owner, project, task_id, **fields):
    """Record explicit test attestations through the public completion guard."""
    from test_completion import evidence

    task = create(store, owner, project, task_id, acceptance_criteria=['accepted result'], **fields)
    body = evidence()
    body['generation'] = task['metadata'].get('_skybuild_workflow', {}).get('generation', 0)
    body['acceptance'] = [{'criterion': criterion, 'evidence_ref': 'test/accepted'} for criterion in task['acceptance_criteria']]
    return store.complete_task(owner, project, task_id, body, task['revision'], 'complete-' + task_id)


def ready(store, owner, project, task, key=None):
    return store.task_action(owner, project, task['task_id'], 'ready',
                             {'reason': 'Definition and prerequisites checked'},
                             task['revision'], key or uuid4().hex)


def test_current_dependency_ready_and_transitive_invalidation(store, actors):
    project, people = actors
    owner = people['owner']
    prerequisite = completed_fixture(store, owner, project, 'prerequisite')
    dependent = create(store, owner, project, 'dependent', dependencies=['prerequisite'], acceptance_criteria=['check'])
    tail = create(store, owner, project, 'tail', dependencies=['dependent'])
    evaluated = ready(store, owner, project, dependent, 'ready-dependent')
    assert evaluated['status'] == 'ready'
    assert evaluated['metadata']['_skybuild_workflow']['readiness'] == {'input_generation': 2, 'assessed_generation': 2}
    store.task_action(owner, project, 'prerequisite', 'rework', {'reason': 'Completion reopened'},
                      prerequisite['revision'], 'reopen')
    for task_id in ('dependent', 'tail'):
        invalidated = store.get_task(owner, project, task_id)
        assert (invalidated['status'], invalidated['phase']) == ('blocked', 'reassess')
        marker = invalidated['metadata']['_skybuild_workflow']['readiness']
        assert marker['input_generation'] > marker['assessed_generation']
        history = store.task_history(owner, project, task_id)
        assert history[-1]['operation'] == 'dependency_invalidated'
        assert history[-1]['after_state'] == invalidated
    # Retry returns its old historical result, but cannot restore the current projection.
    assert ready(store, owner, project, dependent, 'ready-dependent') == evaluated
    current = store.get_task(owner, project, 'dependent')
    assert current['status'] == 'blocked'
    with pytest.raises(DomainError, match='lacks current completion'):
        ready(store, owner, project, current)
    assert store.get_task(owner, project, 'tail')['revision'] == tail['revision'] + 2


def test_dependency_edge_edit_invalidates_transitive_tail(store, actors):
    project, people = actors
    owner = people['owner']
    completed_fixture(store, owner, project, 'complete')
    create(store, owner, project, 'unmet')
    task = create(store, owner, project, 'middle', dependencies=['complete'], acceptance_criteria=['check'])
    task = ready(store, owner, project, task)
    create(store, owner, project, 'tail', dependencies=['middle'])
    changed = store.update_task(owner, project, 'middle', {'dependencies': ['unmet']}, task['revision'], 'new-edge')
    assert changed['status'] == 'blocked'
    assert store.get_task(owner, project, 'tail')['phase'] == 'reassess'
    with pytest.raises(DomainError, match='lacks current completion'):
        ready(store, owner, project, changed)


def test_deferred_milestone_invalidation_preserves_trigger(store, actors):
    project, people = actors
    owner = people['owner']
    milestone = create(store, owner, project, 'milestone')
    task = create(store, owner, project, 'deferred')
    task = store.task_action(owner, project, 'deferred', 'defer',
                             {'reason': 'Wait for milestone', 'milestone_task_id': 'milestone'}, task['revision'], 'defer')
    store.update_task(owner, project, 'milestone', {'description': 'Changed milestone'}, milestone['revision'], 'edit')
    current = store.get_task(owner, project, 'deferred')
    assert (current['status'], current['phase']) == ('deferred', 'deferred')
    assert current['metadata']['_skybuild_workflow']['deferral'] == task['metadata']['_skybuild_workflow']['deferral']
    assert current['revision'] == task['revision'] + 1


def test_input_change_waiting_during_ready_leaves_new_generation_dirty(store, actors, monkeypatch):
    project, people = actors
    owner = people['owner']
    prerequisite = completed_fixture(store, owner, project, 'prerequisite')
    task = create(store, owner, project, 'dependent', dependencies=['prerequisite'], acceptance_criteria=['check'])
    evaluated, release = Event(), Event()
    original = store._replace_task

    def pause_after_evaluation(connection, principal, project_id, task_id, before, changes, **kwargs):
        if project_id == project and task_id == 'dependent' and kwargs['operation'] == 'ready':
            evaluated.set()
            assert release.wait(3)
        return original(connection, principal, project_id, task_id, before, changes, **kwargs)

    monkeypatch.setattr(store, '_replace_task', pause_after_evaluation)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assessment = pool.submit(ready, store, owner, project, task)
        assert evaluated.wait(3)
        edit = pool.submit(store.update_task, owner, project, 'prerequisite',
                           {'description': 'Definition changed during assessment'}, prerequisite['revision'], 'concurrent-edit')
        release.set()
        assessment.result(timeout=5)
        edit.result(timeout=5)
    current = store.get_task(owner, project, 'dependent')
    marker = current['metadata']['_skybuild_workflow']['readiness']
    assert current['status'] == 'blocked'
    assert marker['input_generation'] == marker['assessed_generation'] + 1
    assert [event['operation'] for event in store.task_history(owner, project, 'dependent')] == ['created', 'ready', 'dependency_invalidated']


def test_done_label_without_assessed_inputs_does_not_satisfy_dependency(store, actors):
    project, people = actors
    owner = people['owner']
    create(store, owner, project, 'unassessed')
    task = create(store, owner, project, 'dependent', dependencies=['unassessed'], acceptance_criteria=['check'])
    with store._connection() as connection:
        connection.execute("UPDATE tasks SET status = 'done' WHERE project_id = %s AND task_id = 'unassessed'", (project,))
    with pytest.raises(DomainError, match='lacks current completion'):
        ready(store, owner, project, task)
    assert store.get_task(owner, project, 'dependent')['revision'] == task['revision']


def test_merge_internal_edges_keep_contiguous_journal_snapshots(store, actors):
    project, people = actors
    owner = people['owner']
    a = create(store, owner, project, 'a', acceptance_criteria=['A'])
    b = create(store, owner, project, 'b', dependencies=['a'], acceptance_criteria=['B'])
    result = store.merge_tasks(owner, project, ['a', 'b'],
        {'task_id': 'merged', 'title': 'Merged', 'description': 'Both scopes', 'acceptance_criteria': ['A', 'B'],
         'dependencies': [], 'architecture_refs': []}, [], {'a': a['revision'], 'b': b['revision']},
        'Combine scopes', 'merge')
    assert all(task['status'] == 'superseded' for task in result['sources'])
    history = store.task_history(owner, project, 'b')
    assert [event['operation'] for event in history] == ['created', 'dependency_invalidated', 'merge']
    assert history[-1]['before_state'] == history[-2]['after_state']
    assert history[-1]['after_state'] == store.get_task(owner, project, 'b')


@pytest.mark.parametrize('action', ['split', 'merge'])
def test_structural_response_and_retry_use_final_reverse_order_projections(store, actors, action):
    project, people = actors
    owner = people['owner']
    create(store, owner, project, 'a', acceptance_criteria=['A'])
    create(store, owner, project, 'b', acceptance_criteria=['B'])
    create(store, owner, project, 'x', dependencies=['a'])
    create(store, owner, project, 'y', dependencies=['a', 'x'])
    if action == 'merge':
        target = {'task_id': 'merged', 'title': 'Merged', 'description': 'Both scopes',
                  'acceptance_criteria': ['A', 'B'], 'dependencies': [], 'architecture_refs': []}
        def apply():
            return store.merge_tasks(owner, project, ['a', 'b'], target, ['y', 'x'],
                                     {'a': 1, 'b': 1}, 'Combine scopes', 'reverse-merge')
    else:
        children = [
            {'task_id': 'child-a', 'title': 'Child A', 'description': 'Scope A',
             'acceptance_criteria': ['A'], 'architecture_refs': [], 'dependencies': []},
            {'task_id': 'child-b', 'title': 'Child B', 'description': 'Scope B',
             'acceptance_criteria': ['A'], 'architecture_refs': [], 'dependencies': []},
        ]
        def apply():
            return store.split_task(owner, project, 'a', children,
                                    {'y': ['child-a'], 'x': ['child-b']}, 'Separate scopes', 1, 'reverse-split')
    result = apply()
    replay = apply()
    assert replay == result
    assert [task['task_id'] for task in result['rewired']] == ['y', 'x']
    for task in result['rewired']:
        assert task == store.get_task(owner, project, task['task_id'])
        assert task == store.task_history(owner, project, task['task_id'])[-1]['after_state']
    assert result['rewired'][0]['revision'] == 3
    for key, value in result.items():
        for task in value if isinstance(value, list) else [value]:
            assert task == store.get_task(owner, project, task['task_id'])


def attest(store, owner, project, task_id, key):
    from test_completion import evidence

    task = store.get_task(owner, project, task_id)
    body = evidence()
    body['generation'] = task['metadata'].get('_skybuild_workflow', {}).get('generation', 0)
    body['acceptance'] = [{'criterion': criterion, 'evidence_ref': 'test/accepted'} for criterion in task['acceptance_criteria']]
    return store.complete_task(owner, project, task_id, body, task['revision'], key)


def test_reopened_acceptance_invalidates_completed_transitive_dependents(store, actors):
    from skybuild.completion import current_completion

    project, people = actors
    owner = people['owner']
    completed_fixture(store, owner, project, 'root')
    completed_fixture(store, owner, project, 'middle', dependencies=['root'])
    completed_fixture(store, owner, project, 'tail', dependencies=['middle'])
    root = store.get_task(owner, project, 'root')
    store.task_action(owner, project, 'root', 'rework', {'reason': 'Acceptance requires correction'},
                      root['revision'], 'reopen-accepted')
    for task_id in ('root', 'middle', 'tail'):
        assert not current_completion(store.get_task(owner, project, task_id))
        assert any(current_completion(event['after_state']) for event in store.task_history(owner, project, task_id))
    with pytest.raises(DomainError, match='lacks current completion'):
        attest(store, owner, project, 'tail', 'tail-too-soon')
    attest(store, owner, project, 'root', 'root-corrected')
    with pytest.raises(DomainError, match='lacks current completion'):
        attest(store, owner, project, 'tail', 'middle-not-current')
    attest(store, owner, project, 'middle', 'middle-reassessed')
    tail = attest(store, owner, project, 'tail', 'tail-reassessed')
    assert current_completion(tail)
    marker = tail['metadata']['_skybuild_workflow']['readiness']
    assert marker['input_generation'] == marker['assessed_generation']


@pytest.mark.parametrize('missing', ['attestation', 'freshness'])
def test_ready_and_completion_require_both_attestation_and_freshness(store, actors, missing):
    project, people = actors
    owner = people['owner']
    completed_fixture(store, owner, project, 'prerequisite')
    task = create(store, owner, project, 'dependent', dependencies=['prerequisite'], acceptance_criteria=['check'])
    with store._connection() as connection:
        if missing == 'attestation':
            connection.execute("UPDATE tasks SET metadata = metadata - '_skybuild_completion' "
                               "WHERE project_id = %s AND task_id = 'prerequisite'", (project,))
        else:
            connection.execute("UPDATE task_readiness SET input_generation = input_generation + 1 "
                               "WHERE project_id = %s AND task_id = 'prerequisite'", (project,))
    with pytest.raises(DomainError, match='lacks current completion'):
        ready(store, owner, project, task)
    with pytest.raises(DomainError, match='lacks current completion'):
        attest(store, owner, project, 'dependent', 'complete-with-missing-input')
    assert store.get_task(owner, project, 'dependent')['revision'] == task['revision']
    assert len(store.task_history(owner, project, 'dependent')) == 1
