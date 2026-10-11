"""Real REST producer, Store transaction and kernel on an owned disposable DB."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from skybuild.api import create_app
from skybuild.client import Client
from skybuild.task_unblocker import run
from test_store import actors, create, store


def test_taskunblocker_attaches_future_deferral_evidence_without_release(store, actors, tmp_path):
    project, people = actors
    task = create(store, people['owner'], project, 'unblocker-http', acceptance_criteria=['Keep the explicit deferral'])
    view = store.initialize_workflow(people['owner'], project, task['task_id'], task['revision'], 'unblocker-initialize')
    until = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    deferred = store.workflow_transition(people['owner'], project, task['task_id'],
        event='defer', body={'until':until,'reason':'Wait for the explicit UTC date','next_action':'Recheck the date'},
        expected_revision=view['task']['revision'], idempotency_key='unblocker-defer')
    original_revision = deferred['task']['revision']
    with TestClient(create_app(store)) as web:
        with Client('http://testserver', people['owner_token'], retries=0, trust_env=False, transport=web._transport) as client:
            args = dict(apply=True,expected_principal=people['owner'].principal_id,api_url='http://testserver')
            first = run(client,project,tmp_path/'state',**args)
            assert first['actions'] == [{'task_id':task['task_id'],'outcome':'confirmed_history'}]
            current = client.task_workflow(project,task['task_id'])
            assert current['token']['place'] == 'deferred'
            assert current['token']['deferred_until'] == until
            assert current['task']['revision'] == original_revision + 1
            assert current['token']['blocker'].startswith('Wait for the explicit UTC date')
            assert 'TaskUnblocker verified at ' in current['token']['blocker']
            history = client.task_history(project,task['task_id'])
            event = history[-1]
            assert event['operation'] == 'workflow.update_control'
            assert event['event_facts']['from_place'] == event['event_facts']['to_place'] == 'deferred'
            assert event['event_facts']['reason'] == current['token']['blocker']
            second = run(client,project,tmp_path/'state',**args)
            assert second['findings'] == []
            assert client.task_workflow(project,task['task_id'])['task']['revision'] == original_revision + 1
            assert len(client.task_history(project,task['task_id'])) == len(history)
