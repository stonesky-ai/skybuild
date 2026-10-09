"""Scoped cached execution snapshots against disposable PostgreSQL only."""
from contextlib import contextmanager
import json
from pathlib import Path
from uuid import uuid4

import pytest
import skybuild

from skybuild.contracts import DomainError
from skybuild.execution_status import ExecutionStatus
from skybuild.store import Store
from test_store import store, actors  # noqa: F401: disposable fixtures
from test_admission import setup
from test_claims import expire
from test_observations import packet

# Never accidentally validate an installed or neighboring checkout's source.
assert Path(skybuild.__file__).resolve().parents[2] == Path(__file__).resolve().parents[1]


def view(store, principal, project, task_id, **kwargs):
    return ExecutionStatus.execution_status(store, principal, project, task_id, **kwargs)


def effect(store, owner, project, request, suffix):
    body = dict(operation_id=uuid4().hex, attempt_id=request['attempt_id'],
                authority_epoch=123, authority_generation=456, input_digest='c' * 64,
                policy_digest='d' * 64, allocation_refs=['private-allocation'])
    return store.create_effect_intent(owner, project, request['task_id'], body,
                                      request['expected_revision'], 'effect-' + suffix, claim_fence=1)


def captured_state(store, project):
    tables = ('tasks', 'task_claims', 'cpu_reservations', 'task_effects',
              'observation_events', 'observation_projections', 'claim_journal',
              'cpu_journal', 'task_journal')
    with store._connection() as connection:
        captured = {table: connection.execute('SELECT to_jsonb(t) AS row FROM ' + table +
                                             ' t WHERE project_id = %s ORDER BY to_jsonb(t)::text',
                                             (project,)).fetchall() for table in tables}
        # Effect journals inherit project identity through their immutable
        # operation reference; the journal has no project_id column.
        captured['effect_journal'] = connection.execute(
            'SELECT to_jsonb(j) AS row FROM effect_journal j '
            'JOIN task_effects e ON e.operation_id = j.operation_id '
            'WHERE e.project_id = %s ORDER BY to_jsonb(j)::text', (project,)).fetchall()
        return captured


@pytest.mark.parametrize('limit', [0, -1, 101, True, 1.5, '20'])
def test_invalid_limit_refused_without_database(limit):
    class NoDatabase:
        _page = staticmethod(Store._page)

        def _connection(self):
            pytest.fail('Invalid bound reached the database')

    with pytest.raises(DomainError) as caught:
        view(NoDatabase(), None, 'project', 'task', limit=limit)
    assert caught.value.code == 'validation'


def test_expired_claim_reported_exit_and_other_task_stay_held(store, actors):
    project, people = actors
    requests = [setup(store, people, project, task_id, capacity=2, actor='owner')
                for task_id in ('first', 'second')]
    for request in requests:
        store.reserve_cpu(people['owner'], project, **request)
        effect(store, people['owner'], project, request, request['task_id'])
    first, second = requests
    evidence = dict(packet(first), state='exited', boot_id='private-boot',
                    process_start='/run/secrets/private-process', evidence_refs=['private-artifact'])
    store.record_observation(people['owner'], project, evidence)
    expire(store, project, first['task_id'])
    before = captured_state(store, project)
    result = view(store, people['worker'], project, first['task_id'])
    assert result['claim']['held'] is True
    assert result['reservations']['items'][0]['state'] == 'reserved'
    assert result['effects']['items'][0]['exposure_held'] is True
    assert result['observations']['items'][0]['state'] == 'exited'
    serialized = json.dumps(result)
    for hidden in ('private-boot', 'private-process', 'private-artifact', 'private-allocation',
                   'c' * 64, 'd' * 64, second['attempt_id'], second['action_id']):
        assert hidden not in serialized
    assert set(result) == {'task_id', 'revision', 'claim', 'reservations', 'effects', 'observations'}
    assert set(result['claim']) == {'fence', 'holder', 'task_revision', 'lease_until', 'held'}
    assert set(result['reservations']['items'][0]) == {
        'action_id', 'attempt_id', 'actor', 'claim_fence', 'task_revision', 'units', 'state'}
    assert set(result['effects']['items'][0]) == {
        'operation_id', 'attempt_id', 'task_revision', 'state', 'exposure_held', 'created_at'}
    assert set(result['observations']['items'][0]) == {
        'event_id', 'attempt_id', 'component_id', 'source_id', 'source_sequence',
        'observed_at', 'received_at', 'state'}
    assert captured_state(store, project) == before
    other = view(store, people['worker'], project, second['task_id'])
    assert other['claim']['held'] is True
    assert other['reservations']['items'][0]['attempt_id'] == second['attempt_id']
    assert other['observations'] == {'items': [], 'truncated': False}
    assert captured_state(store, project) == before


def test_each_section_truncates_independently_in_deterministic_order(store, actors):
    project, people = actors
    request = setup(store, people, project, actor='owner')
    for index in range(22):
        current = dict(request, action_id=f'action-{index:02}-' + uuid4().hex,
                       attempt_id=uuid4().hex)
        store.reserve_cpu(people['owner'], project, **current)
        if index < 21:
            store.cancel_cpu_reservation(people['owner'], project, current['action_id'], reason='Fixture never dispatched')
    for index in range(22):
        effect(store, people['owner'], project, current, str(index))
        evidence = dict(packet(current), component_id=f'component-{index:02}',
                        boot_id='sensitive-boot', process_start='sensitive-start')
        store.record_observation(people['owner'], project, evidence)
    result = view(store, people['worker'], project, request['task_id'])
    for section, key in (('reservations', 'action_id'), ('effects', 'operation_id'),
                         ('observations', 'component_id')):
        items = result[section]['items']
        assert len(items) == 20 and result[section]['truncated'] is True
        assert [item[key] for item in items] == sorted(item[key] for item in items)
    assert result == view(store, people['worker'], project, request['task_id'])
    maximum = view(store, people['worker'], project, request['task_id'], limit=100)
    for section in ('reservations', 'effects', 'observations'):
        assert len(maximum[section]['items']) == 22
        assert maximum[section]['truncated'] is False


def test_missing_claim_empty_sections_and_authorization_before_lookup(store, actors):
    project, people = actors
    store.create_task(people['owner'], project, {'task_id': 'plain', 'title': 'Plain',
                      'description': 'Private metadata', 'metadata': {'secret': 'never expose'}}, 'plain')
    result = view(store, people['worker'], project, 'plain')
    assert result['claim'] is None
    assert all(result[section] == {'items': [], 'truncated': False}
               for section in ('reservations', 'effects', 'observations'))
    assert 'never expose' not in json.dumps(result)
    for task_id in ('plain', 'missing'):
        with pytest.raises(DomainError) as caught:
            view(store, people['outsider'], project, task_id)
        assert caught.value.status_code == 403
    with pytest.raises(DomainError) as caught:
        view(store, people['worker'], project, 'missing')
    assert caught.value.status_code == 404
    token, principal = uuid4().hex, 'claim-only-' + uuid4().hex
    store.provision_principal(principal, token, grants={project: {'tasks:claim'}})
    claim_only = store.authenticate(token)
    with pytest.raises(DomainError) as caught:
        view(store, claim_only, project, 'plain')
    assert caught.value.status_code == 403
    # A previously authenticated principal cannot retain a revoked read grant.
    store.provision_principal(people['worker'].principal_id, people['worker_token'], grants={})
    with pytest.raises(DomainError) as caught:
        view(store, people['worker'], project, 'plain')
    assert caught.value.status_code == 403


def test_same_task_id_other_project_never_enters_snapshot(store, actors):
    project, people = actors
    request = setup(store, people, project, task_id='shared-id')
    store.reserve_cpu(people['worker'], project, **request)
    other_project = 'other-' + uuid4().hex
    other = setup(store, people, other_project, task_id='shared-id', actor='owner')
    store.reserve_cpu(people['owner'], other_project, **other)
    result = view(store, people['worker'], project, 'shared-id')
    assert len(result['reservations']['items']) == 1
    assert result['reservations']['items'][0]['attempt_id'] == request['attempt_id']
    assert other['attempt_id'] not in json.dumps(result)
    with pytest.raises(DomainError) as caught:
        view(store, people['worker'], other_project, 'shared-id')
    assert caught.value.status_code == 403


def test_snapshot_cannot_tear_claim_and_reservation_during_atomic_change(store, actors, monkeypatch):
    project, people = actors
    request = setup(store, people, project)
    store.reserve_cpu(people['worker'], project, **request)
    original = store._connection
    changed = False

    class InterleavingConnection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, statement, parameters=None):
            nonlocal changed
            cursor = self.connection.execute(statement, parameters)
            if not changed and 'FROM task_claims' in statement:
                changed = True
                # Commit immediately after the query has materialized its claim
                # data. A later reservation query would see the new state.
                with original() as writer:
                    writer.execute("UPDATE task_claims SET held = false, claim_revision = claim_revision + 1 WHERE project_id = %s AND task_id = %s",
                                   (project, request['task_id']))
                    writer.execute("UPDATE cpu_reservations SET state = 'cancelled' WHERE project_id = %s AND action_id = %s",
                                   (project, request['action_id']))
            return cursor

    @contextmanager
    def interleaved():
        with original() as connection:
            yield InterleavingConnection(connection)

    monkeypatch.setattr(store, '_connection', interleaved)
    result = view(store, people['worker'], project, request['task_id'])
    assert changed is True
    assert result['claim']['held'] is True
    assert result['reservations']['items'][0]['state'] == 'reserved'
    latest = view(store, people['worker'], project, request['task_id'])
    assert latest['claim']['held'] is False
    assert latest['reservations']['items'][0]['state'] == 'cancelled'


def test_restricted_role_api_auth_scope_bounds_and_missing_task(restricted_database):
    from fastapi.testclient import TestClient
    from skybuild.api import create_app

    admin_dsn, runtime_dsn, database, _ = restricted_database
    admin, runtime = Store(admin_dsn, database), Store(runtime_dsn, database)
    project = 'status-' + uuid4().hex
    identities, tokens = {}, {}
    for name, grants in (('owner', {}), ('reader', {project: {'tasks:read'}}),
                         ('claim-only', {project: {'tasks:claim'}}), ('outsider', {})):
        principal, token = name + '-' + uuid4().hex, uuid4().hex
        admin.provision_principal(principal, token, is_admin=name == 'owner', grants=grants)
        identities[name], tokens[name] = admin.authenticate(token), token
    request = setup(admin, identities, project, task_id='restricted-status', actor='owner')
    admin.reserve_cpu(identities['owner'], project, **request)
    effect(admin, identities['owner'], project, request, 'restricted')
    before = captured_state(admin, project)
    url = f'/api/v1/projects/{project}/tasks/restricted-status/execution-status'

    def headers(name):
        return {'Authorization': 'Bearer ' + tokens[name]}

    with TestClient(create_app(runtime)) as client:
        assert client.get(url).status_code == 401
        assert client.get(url, headers={'Authorization': 'Bearer ' + 'invalid-token' * 4}).status_code == 401
        response = client.get(url, headers=headers('reader'))
        assert response.status_code == 200
        assert response.json()['reservations']['items'][0]['attempt_id'] == request['attempt_id']
        for name in ('outsider', 'claim-only'):
            assert client.get(url, headers=headers(name)).status_code == 403
            assert client.get(url.replace('restricted-status', 'absent-task'), headers=headers(name)).status_code == 403
        assert client.get(url.replace(project, 'other-project'), headers=headers('reader')).status_code == 403
        assert client.get(url.replace('restricted-status', 'absent-task'), headers=headers('reader')).status_code == 404
        assert client.get(url + '?limit=100', headers=headers('reader')).status_code == 200
        for limit in ('0', '101', 'true', '1.5'):
            assert client.get(url + '?limit=' + limit, headers=headers('reader')).status_code == 422
        admin.provision_principal(identities['reader'].principal_id, tokens['reader'], grants={})
        assert client.get(url, headers=headers('reader')).status_code == 403
    assert captured_state(admin, project) == before


def test_database_error_is_503_not_empty_success(monkeypatch):
    import psycopg
    from fastapi.testclient import TestClient
    from skybuild.api import create_app
    from skybuild.contracts import Principal

    store = Store('unused', 'skybuild_test')
    monkeypatch.setattr(store, 'authenticate', lambda token: Principal('reader', False, {'project': frozenset({'tasks:read'})}))

    def unavailable(*args, **kwargs):
        raise psycopg.OperationalError('SECRET connection details')

    monkeypatch.setattr('skybuild.store.psycopg.connect', unavailable)
    with TestClient(create_app(store)) as client:
        response = client.get('/api/v1/projects/project/tasks/task/execution-status',
                              headers={'Authorization': 'Bearer ' + 'x' * 32})
    assert response.status_code == 503
    assert response.json()['error']['code'] == 'unavailable'
    assert 'SECRET' not in response.text


def test_shared_client_uses_read_only_scoped_route():
    import httpx
    from skybuild.client import Client

    requests = []

    def serve(request):
        requests.append(request)
        return httpx.Response(200, json={'task_id': 'task space', 'revision': 1})

    with Client('https://skybuild.test', 'token', transport=httpx.MockTransport(serve)) as client:
        assert client.execution_status('project space', 'task space', limit=7)['revision'] == 1
    assert len(requests) == 1
    request = requests[0]
    assert request.method == 'GET'
    assert request.url.raw_path == b'/api/v1/projects/project%20space/tasks/task%20space/execution-status?limit=7'
    assert 'Idempotency-Key' not in request.headers
    assert 'If-Match' not in request.headers


@pytest.mark.parametrize('limit', [None, 7])
def test_cli_reads_one_task_and_preserves_ca_option(monkeypatch, capsys, tmp_path, limit):
    from skybuild import __main__ as cli
    calls = []
    ca = tmp_path / 'installation-ca.pem'

    class FakeClient:
        def __init__(self, url, token, *, ca_file):
            calls.append((url, token, ca_file))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execution_status(self, project, task, *, limit):
            calls.append((project, task, limit))
            return {'task_id': task, 'revision': 2}

    monkeypatch.setattr(cli, 'Client', FakeClient)
    monkeypatch.setenv('SKYBUILD_API_URL', 'https://private.test')
    monkeypatch.setenv('SKYBUILD_TOKEN', 'private-token')
    arguments = ['execution-status', 'project', 'task', '--ca-file', str(ca)]
    if limit is not None:
        arguments += ['--limit', str(limit)]
    assert cli.main(arguments) == 0
    assert calls == [('https://private.test', 'private-token', ca), ('project', 'task', 20 if limit is None else limit)]
    output = capsys.readouterr()
    assert json.loads(output.out)['revision'] == 2
    assert 'private-token' not in output.out + output.err
