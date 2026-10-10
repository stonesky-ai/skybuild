"""Claim capability uses the real ownership guards without allocating work."""
from uuid import uuid4
import re

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from skybuild.api import create_app
from skybuild.contracts import Principal
from skybuild.store import Store
from skybuild.workflow import TaskWorkflow, TaskToken, Place
from test_petri_store import enrolled, token_task
from test_store import store, actors


class PreviewConnection:
    def __init__(self, *, claim=None, reservation=False, effect=False):
        self.claim = claim
        self.reservation = reservation
        self.effect = effect
        self.statements = []

    def execute(self, query, parameters):
        self.statements.append(query)
        self.row = None
        if query.startswith('SELECT * FROM task_claims'):
            self.row = self.claim
        elif 'FROM cpu_reservations' in query and self.reservation:
            self.row = {'held': True}
        elif 'FROM task_effects' in query and self.effect:
            self.row = {'held': True}
        return self

    def fetchone(self):
        return self.row


def assert_read_only(statements):
    for query in statements:
        assert query.startswith(('SELECT', 'WITH RECURSIVE'))
        assert not re.search(r'\b(INSERT|UPDATE|DELETE|MERGE|TRUNCATE|CREATE|ALTER|DROP)\b', query, re.I)


def preview_inputs():
    token = TaskToken('project', 'task', Place.READY)
    task = token_task(token)
    task['status'] = 'ready'
    context = {name: getattr(token, name) for name in
               ('source_head', 'target_base', 'definition_revision', 'input_generation', 'policy_version')}
    context.update(current_inputs=True, effects_resolved=True, dependencies_satisfied=True)
    principal = Principal('worker', False, {'project': frozenset({'tasks:read', 'tasks:claim'})})
    return task, context, principal


def test_preview_enables_claim_without_allocating_attempt_or_ownership():
    task, context, principal = preview_inputs()
    connection = PreviewConnection()
    preview = Store('unused', 'unused')._claim_preview_context(connection, principal, task, context)
    assert 'claim' in TaskWorkflow().enabled(Store.workflow_token(task), preview)
    assert 'attempt_id' not in context and 'admission_permitted' not in context
    assert_read_only(connection.statements)
    assert task['metadata']['_skybuild_workflow']['petri']['token']['attempt_id'] is None


@pytest.mark.parametrize('case', ['missing_grant', 'held', 'reservation', 'effect', 'pending', 'superseded', 'stale', 'dependencies', 'exhausted'])
def test_preview_rejects_each_real_claim_blocker(case):
    task, context, principal = preview_inputs()
    options = {}
    if case == 'missing_grant':
        principal = Principal('reader', False, {'project': frozenset({'tasks:read'})})
    elif case == 'held':
        options['claim'] = {'held': True, 'fence': 1}
    elif case == 'reservation':
        options['reservation'] = True
    elif case == 'effect':
        options['effect'] = True
    elif case == 'pending':
        task['metadata']['_skybuild_workflow']['petri']['token']['pending_action'] = 'hold'
    elif case == 'superseded':
        task['status'] = 'superseded'
    elif case == 'stale':
        context['current_inputs'] = False
    elif case == 'dependencies':
        context['dependencies_satisfied'] = False
    elif case == 'exhausted':
        options['claim'] = {'held': False, 'fence': 2**63 - 1}
    connection = PreviewConnection(**options)
    preview = Store('unused', 'unused')._claim_preview_context(connection, principal, task, context)
    assert 'claim' not in TaskWorkflow().enabled(Store.workflow_token(task), preview)
    assert_read_only(connection.statements)


def test_actual_store_and_api_offer_claim_only_to_eligible_principal(store, actors):
    project, people = actors
    task = enrolled(store, people, project)
    persisted_before = store.get_task(people['owner'], project, task['task_id'])
    client = TestClient(create_app(store))
    path = f'/api/v1/projects/{project}/tasks/{task["task_id"]}/workflow'
    response = client.get(path, headers={'Authorization': 'Bearer ' + people['worker_token']})
    assert response.status_code == 200
    assert 'claim' in response.json()['available_actions']
    assert 'claim' in response.json()['task']['enabled_actions']
    assert store.claim_history(people['owner'], project, task['task_id']) == []
    assert store.get_task(people['owner'], project, task['task_id']) == persisted_before
    assert store.task_workflow(people['worker'], project, task['task_id']) == response.json()

    reader_id, reader_token = 'reader-' + uuid4().hex, uuid4().hex + uuid4().hex
    store.provision_principal(reader_id, reader_token, grants={project: {'tasks:read'}})
    response = client.get(path, headers={'Authorization': 'Bearer ' + reader_token})
    assert response.status_code == 200 and 'claim' not in response.json()['available_actions']

    with store._connection() as connection:
        connection.execute("UPDATE tasks SET metadata = jsonb_set(metadata, '{_skybuild_workflow,petri,token,pending_action}', %s) "
                           'WHERE project_id = %s AND task_id = %s', (Jsonb('hold'), project, task['task_id']))
    assert 'claim' not in store.task_workflow(people['worker'], project, task['task_id'])['available_actions']
    with store._connection() as connection:
        connection.execute("UPDATE tasks SET metadata = metadata #- '{_skybuild_workflow,petri,token,pending_action}' "
                           'WHERE project_id = %s AND task_id = %s', (project, task['task_id']))
    store.claim_task(people['worker'], project, task['task_id'], task['revision'], 'claim-real')
    response = client.get(path, headers={'Authorization': 'Bearer ' + people['worker_token']})
    assert response.status_code == 200 and 'claim' not in response.json()['available_actions']
