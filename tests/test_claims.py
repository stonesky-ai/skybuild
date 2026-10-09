"""Exclusive ownership tests against task-owned disposable PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from skybuild.api import create_app
from skybuild.contracts import DomainError
from test_store import actors, create, store


def ready(store, people, project, task_id='claim-task'):
    task = create(store, people['owner'], project, task_id, acceptance_criteria=['Retain ownership history'])
    return store.task_action(people['owner'], project, task_id, 'ready', {'reason': 'Reviewed'}, 1, 'ready-' + task_id)


def expire(store, project, task_id):
    with store._connection() as connection:
        connection.execute("UPDATE task_claims SET lease_until = clock_timestamp() - interval '1 second', claim_revision = claim_revision + 1 "
                           'WHERE project_id = %s AND task_id = %s', (project, task_id))


def test_concurrent_claims_one_winner_and_stable_replay(store, actors):
    project, people = actors
    task = ready(store, people, project)
    barrier = Barrier(2)
    def claim(name):
        barrier.wait()
        try:
            return store.claim_task(people[name], project, task['task_id'], task['revision'], name)
        except DomainError as error:
            return error
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ['worker', 'peer']))
    winner = next(result for result in results if isinstance(result, dict))
    loser = next(result for result in results if isinstance(result, DomainError))
    assert winner['fence'] == 1 and loser.code == 'claim_conflict'
    name = 'worker' if winner['holder'] == people['worker'].principal_id else 'peer'
    assert store.claim_task(people[name], project, task['task_id'], task['revision'], name) == winner
    assert len(store.claim_history(people['owner'], project, task['task_id'])) == 1


def test_expiry_does_not_release_and_stale_fence_cannot_renew_or_release(store, actors):
    project, people = actors
    task = ready(store, people, project)
    claim = store.claim_task(people['worker'], project, task['task_id'], 2, 'claim')
    expire(store, project, task['task_id'])
    for action in (
        lambda: store.claim_task(people['peer'], project, task['task_id'], 2, 'steal'),
        lambda: store.renew_claim(people['worker'], project, task['task_id'], 1, 2, 'renew'),
        lambda: store.release_claim(people['worker'], project, task['task_id'], 1, 2, 'release', reason='Expired'),
        lambda: store.update_task(people['owner'], project, task['task_id'], {'description': 'Changed'}, 2, 'edit'),
    ):
        with pytest.raises(DomainError) as caught:
            action()
        assert caught.value.code == 'claim_conflict'
    with pytest.raises(DomainError) as caught:
        store.reconcile_claim(people['peer'], project, task['task_id'], 1, 2, 'reconcile', reason='No launch exists')
    assert caught.value.code == 'authorization'
    released = store.reconcile_claim(people['owner'], project, task['task_id'], 1, 2, 'reconcile',
                                     reason='Launch-free test: no process or external dispatch exists')
    assert not released['held']
    second = store.claim_task(people['peer'], project, task['task_id'], 2, 'second')
    assert second['fence'] == 2
    with pytest.raises(DomainError):
        store.renew_claim(people['worker'], project, task['task_id'], claim['fence'], 2, 'late')
    assert store.claim_task(people['worker'], project, task['task_id'], 2, 'claim') == claim
    assert [e['action'] for e in store.claim_history(people['owner'], project, task['task_id'])] == ['claim', 'reconcile', 'claim']


def test_renew_release_scope_revision_and_grant(store, actors):
    project, people = actors
    task = ready(store, people, project)
    for principal, scope, revision, code in (
        (people['outsider'], project, 2, 'authorization'),
        (people['worker'], 'other-project', 2, 'authorization'),
        (people['worker'], project, 1, 'stale_revision'),
    ):
        with pytest.raises(DomainError) as caught:
            store.claim_task(principal, scope, task['task_id'], revision, uuid4().hex)
        assert caught.value.code == code
    claim = store.claim_task(people['worker'], project, task['task_id'], 2, 'claim')
    renewed = store.renew_claim(people['worker'], project, task['task_id'], 1, 2, 'renew', lease_seconds=120)
    assert renewed['lease_until'] > claim['lease_until']
    with pytest.raises(DomainError):
        store.release_claim(people['peer'], project, task['task_id'], 1, 2, 'wrong-holder', reason='No')
    released = store.release_claim(people['worker'], project, task['task_id'], 1, 2, 'release', reason='Never launched')
    assert not released['held']
    assert store.release_claim(people['worker'], project, task['task_id'], 1, 2, 'release', reason='Never launched') == released
    assert store.get_task(people['owner'], project, task['task_id']) == task
    assert [event['claim_revision'] for event in store.claim_history(people['owner'], project, task['task_id'])] == [1, 2, 3]


def test_effect_writes_require_current_claim_fence_and_uncertainty_blocks_release(store, actors):
    project, people = actors
    owner = people['owner']
    task = ready(store, people, project)
    store.claim_task(owner, project, task['task_id'], 2, 'claim')
    body = dict(operation_id=uuid4().hex, attempt_id='attempt', authority_epoch=1, authority_generation=1,
                input_digest='a' * 64, policy_digest='b' * 64, allocation_refs=['allocation'])
    for fence in (None, 2):
        with pytest.raises(DomainError) as caught:
            store.create_effect_intent(owner, project, task['task_id'], body, 2, uuid4().hex, claim_fence=fence)
        assert caught.value.code == 'claim_conflict'
    effect = store.create_effect_intent(owner, project, task['task_id'], body, 2, 'intent', claim_fence=1)
    with pytest.raises(DomainError):
        store.observe_effect(owner, project, effect['operation_id'], 'unknown', 'Lost reply', 'bad')
    store.observe_effect(owner, project, effect['operation_id'], 'unknown', 'Lost reply', 'unknown', claim_fence=1)
    expire(store, project, task['task_id'])
    with pytest.raises(DomainError) as caught:
        store.reconcile_claim(owner, project, task['task_id'], 1, 2, 'reconcile', reason='Lease expired')
    assert caught.value.code == 'effect_conflict'


def test_claim_journal_immutable(store, actors):
    project, people = actors
    task = ready(store, people, project)
    store.claim_task(people['worker'], project, task['task_id'], 2, 'claim')
    for statement in ('UPDATE claim_journal SET reason = reason', 'DELETE FROM claim_journal', 'TRUNCATE claim_journal',
                      'DELETE FROM task_claims', 'TRUNCATE task_claims', 'UPDATE task_claims SET fence = fence + 1'):
        with pytest.raises(DomainError) as caught:
            with store._connection() as connection:
                connection.execute(statement)
        assert isinstance(caught.value.__cause__, psycopg.Error)


def test_claim_markdown_authority_guard_and_revoked_grant(store, actors):
    project, people = actors
    task = ready(store, people, project)
    with store._connection() as connection:
        connection.execute("UPDATE ledger_imports SET authority = 'markdown' WHERE project_id = %s", (project,))
    with pytest.raises(DomainError) as caught:
        store.claim_task(people['owner'], project, task['task_id'], 2, 'blocked-import')
    assert caught.value.code == 'authority'
    second_project = project + '-revoked'
    from test_store import seed_api_authority
    seed_api_authority(store, second_project)
    task = ready(store, people, second_project)
    with store._connection() as connection:
        connection.execute('DELETE FROM principal_grants WHERE principal_id = %s AND operation = %s',
                           (people['worker'].principal_id, 'tasks:claim'))
    with pytest.raises(DomainError) as caught:
        store.claim_task(people['worker'], project, 'claim-task', 2, 'revoked')
    assert caught.value.code == 'authorization'


def test_claim_racing_definition_edit_serializes_or_rejects_stale_revision(store, actors):
    project, people = actors
    task = ready(store, people, project)
    barrier = Barrier(2)
    def mutate(action):
        barrier.wait()
        try:
            if action == 'claim':
                return store.claim_task(people['worker'], project, task['task_id'], 2, 'claim')
            return store.update_task(people['owner'], project, task['task_id'], {'description': 'Revised'}, 2, 'edit')
        except DomainError as error:
            return error
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(mutate, ['claim', 'edit']))
    assert sum(isinstance(result, DomainError) for result in results) == 1
    assert next(result for result in results if isinstance(result, DomainError)).code in {'claim_conflict', 'stale_revision'}


def test_renew_racing_release_cannot_resurrect_ownership(store, actors):
    project, people = actors
    task = ready(store, people, project)
    worker = people['worker']
    store.claim_task(worker, project, task['task_id'], 2, 'claim')
    barrier = Barrier(2)
    def mutate(action):
        barrier.wait()
        try:
            if action == 'renew':
                return store.renew_claim(worker, project, task['task_id'], 1, 2, 'renew')
            return store.release_claim(worker, project, task['task_id'], 1, 2, 'release', reason='Never launched')
        except DomainError as error:
            return error
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(mutate, ['renew', 'release']))
    assert isinstance(results[1], dict) and not results[1]['held']
    with store._connection() as connection:
        assert not connection.execute('SELECT held FROM task_claims WHERE project_id = %s AND task_id = %s',
                                      (project, task['task_id'])).fetchone()['held']


def test_claim_http_validation_and_roundtrip(store, actors):
    project, people = actors
    task = ready(store, people, project)
    client = TestClient(create_app(store))
    base = f'/api/v1/projects/{project}/tasks/{task["task_id"]}/claim'
    headers = {'Authorization': 'Bearer ' + people['worker_token'], 'If-Match': '2', 'Idempotency-Key': 'claim-http'}
    assert client.post(base, headers=headers, json={'lease_seconds': True}).status_code == 422
    response = client.post(base, headers=headers, json={'lease_seconds': 60})
    assert response.status_code == 200 and response.json()['fence'] == 1
    headers['Idempotency-Key'] = 'renew-http'
    assert client.post(base + '/renew', headers=headers, json={'fence': 1}).status_code == 200
    headers['Idempotency-Key'] = 'release-http'
    assert client.post(base + '/release', headers=headers, json={'fence': 1, 'reason': 'Never launched'}).json()['held'] is False
