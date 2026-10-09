"""Owner controls through the restricted runtime; no live or launch effects."""
from contextlib import contextmanager
import json
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
import pytest
import skybuild

from skybuild.api import create_app
from skybuild.client import Client
from skybuild.contracts import DomainError
from skybuild.store import Store
from test_admission import setup
from test_store import seed_api_authority

assert Path(skybuild.__file__).resolve().parents[2] == Path(__file__).resolve().parents[1]


@pytest.fixture
def controls(restricted_database):
    admin_dsn, runtime_dsn, database, _ = restricted_database
    admin, runtime = Store(admin_dsn, database), Store(runtime_dsn, database)
    project = 'cpu-controls-' + uuid4().hex
    seed_api_authority(admin, project)
    people, tokens = {}, {}
    for name, grants in (('owner', {}), ('worker', {project: {'tasks:read', 'tasks:write', 'tasks:claim'}}),
                         ('outsider', {})):
        principal, token = name + '-' + uuid4().hex, uuid4().hex
        admin.provision_principal(principal, token, is_admin=name == 'owner', grants=grants)
        people[name], tokens[name] = admin.authenticate(token), token
    with TestClient(create_app(runtime)) as client:
        yield admin, runtime, client, project, people, tokens


def headers(tokens, name='owner', key=None):
    result = {'Authorization': 'Bearer ' + tokens[name]}
    if key is not None:
        result['Idempotency-Key'] = key
    return result


def central(**changes):
    return dict(capacity=1, enabled=True, expected_generation=0, reason='  Explicit operator reason  ', **changes)


def events(admin, project):
    with admin._connection() as connection:
        return connection.execute('SELECT actor, action, reason, before_state, after_state FROM cpu_journal '
                                  'WHERE project_id = %s ORDER BY created_at, event_id', (project,)).fetchall()


def test_restricted_runtime_admin_auth_and_authority_fence(controls):
    admin, runtime, client, project, people, tokens = controls
    base = f'/api/v1/projects/{project}/cpu-controls'
    assert client.get(base).status_code == 401
    assert client.post(base + '/central', json=central()).status_code == 401
    for name in ('worker', 'outsider'):
        assert client.get(base, headers=headers(tokens, name)).status_code == 403
        assert client.post(base + '/central', headers=headers(tokens, name, 'denied-' + name), json=central()).status_code == 403
        assert client.post(base + '/local', headers=headers(tokens, name, 'local-denied-' + name),
                           json={'enabled': True, 'expected_generation': 1, 'reason': 'Not owner'}).status_code == 403
    assert client.post(base + '/central', headers=headers(tokens), json=central()).status_code == 422
    assert client.get(base, headers=headers(tokens)).json() == {'project_id': project, 'pool': None, 'held_units': 0}
    assert client.get(base.replace(project, 'other-project'), headers=headers(tokens, 'worker')).status_code == 403
    assert events(admin, project) == []
    with admin._connection() as connection:
        connection.execute("UPDATE ledger_imports SET authority = 'markdown' WHERE project_id = %s", (project,))
    for route, body in (('/central', central()), ('/local', {'enabled': True, 'expected_generation': 0, 'reason': 'No cutover'})):
        response = client.post(base + route, headers=headers(tokens, key=uuid4().hex), json=body)
        assert response.status_code == 409 and response.json()['error']['code'] == 'authority'
    # Inspection does not grant or require an authority switch.
    assert client.get(base, headers=headers(tokens)).status_code == 200
    assert events(admin, project) == []
    # An authenticated cached admin principal cannot retain removed admin rights.
    admin.provision_principal(people['owner'].principal_id, tokens['owner'], grants={project: {'tasks:read', 'tasks:write'}})
    with pytest.raises(DomainError) as caught:
        runtime.cpu_control_status(people['owner'], project)
    assert caught.value.status_code == 403
    assert client.get(base, headers=headers(tokens)).status_code == 403


@pytest.mark.parametrize('route,changes', [
    ('central', {'enabled': 'true'}), ('central', {'enabled': 1}),
    ('central', {'capacity': True}), ('central', {'capacity': -1}),
    ('central', {'capacity': 2**31}), ('central', {'expected_generation': True}),
    ('central', {'expected_generation': -1}), ('central', {'expected_generation': 2**63}),
    ('central', {'reason': ''}), ('central', {'reason': ' ' * 3}),
    ('central', {'reason': 'x' * 4097}), ('central', {'extra': 'not allowed'}),
    ('local', {'capacity': 1}), ('local', {'enabled': 'false'}),
])
def test_strict_inputs_do_not_create_controls_or_journals(controls, route, changes):
    admin, _, client, project, _, tokens = controls
    body = central() if route == 'central' else {'enabled': True, 'expected_generation': 1, 'reason': 'Local'}
    body.update(changes)
    response = client.post(f'/api/v1/projects/{project}/cpu-controls/{route}',
                           headers=headers(tokens, key=uuid4().hex), json=body)
    assert response.status_code == 422
    assert events(admin, project) == []
    assert client.get(f'/api/v1/projects/{project}/cpu-controls', headers=headers(tokens)).json()['pool'] is None


def test_lost_reply_retry_preserves_body_key_and_one_generation_journal(controls, monkeypatch):
    admin, _, server, project, people, tokens = controls
    monkeypatch.setattr('skybuild.client.time.sleep', lambda _: None)
    requests = []

    def transport(request):
        requests.append(request)
        response = server.request(request.method, request.url.raw_path.decode(),
                                  headers=dict(request.headers), content=request.content)
        assert response.status_code == 200
        if len(requests) == 1:
            raise httpx.ReadError('Accepted reply lost', request=request)
        return httpx.Response(response.status_code, json=response.json())

    reason = '  Explicit operator reason  '
    with Client('https://private.test', tokens['owner'], transport=httpx.MockTransport(transport)) as client:
        result = client.configure_cpu_pool(project, 1, True, 0, reason=reason, idempotency_key='stable-control')
    assert len(requests) == 2
    assert {request.headers['Idempotency-Key'] for request in requests} == {'stable-control'}
    assert all(json.loads(request.content) == central() for request in requests)
    assert result['generation'] == 1 and result['local_enabled'] is False and result['local_generation'] == 1
    journal = events(admin, project)
    assert len(journal) == 1 and journal[0]['reason'] == reason
    assert journal[0]['actor'] == people['owner'].principal_id
    response = server.post(f'/api/v1/projects/{project}/cpu-controls/central',
                           headers=headers(tokens, key='stable-control'), json=dict(central(), reason=reason.strip()))
    assert response.status_code == 409 and response.json()['error']['code'] == 'idempotency_conflict'
    response = server.post(f'/api/v1/projects/{project}/cpu-controls/central',
                           headers=headers(tokens, key='different-key'), json=central())
    assert response.status_code == 409 and response.json()['error']['code'] == 'stale_revision'
    assert len(events(admin, project)) == 1


def test_control_intersection_cas_and_held_capacity_are_preserved(controls):
    admin, runtime, client, project, people, tokens = controls
    base = f'/api/v1/projects/{project}/cpu-controls'

    def update(route, body):
        return client.post(base + route, headers=headers(tokens, key=uuid4().hex), json=body)

    created = update('/central', central()).json()
    assert created['local_enabled'] is False
    request = setup(admin, people, project, actor='owner')
    request['local_generation'] = 1
    with pytest.raises(DomainError) as caught:
        runtime.reserve_cpu(people['owner'], project, **request)
    assert caught.value.code == 'control_conflict'
    local = update('/local', {'enabled': True, 'expected_generation': 1, 'reason': 'Local enable'})
    assert local.status_code == 200 and local.json()['local_generation'] == 2
    request['local_generation'] = 2
    reservation = runtime.reserve_cpu(people['owner'], project, **request)
    shrunk = update('/central', dict(central(), capacity=0, expected_generation=1))
    assert shrunk.status_code == 409 and shrunk.json()['error']['code'] == 'capacity_conflict'
    assert update('/central', dict(central(), enabled=False, expected_generation=1)).json()['generation'] == 2
    assert update('/local', {'enabled': False, 'expected_generation': 2, 'reason': 'Local stop'}).json()['local_generation'] == 3
    enabled = update('/central', dict(central(), expected_generation=2))
    assert enabled.status_code == 200 and enabled.json()['local_enabled'] is False
    second = setup(admin, people, project, task_id='second', actor='owner')
    second.update(generation=3, local_generation=3)
    with pytest.raises(DomainError) as caught:
        runtime.reserve_cpu(people['owner'], project, **second)
    assert caught.value.code == 'control_conflict'
    stale_local = update('/local', {'enabled': True, 'expected_generation': 2, 'reason': 'Delayed enable'})
    assert stale_local.status_code == 409 and stale_local.json()['error']['code'] == 'stale_revision'
    status = client.get(base, headers=headers(tokens)).json()
    assert status['held_units'] == 1 and status['pool']['capacity'] == 1
    assert set(status) == {'project_id', 'pool', 'held_units'}
    assert set(status['pool']) == {'project_id', 'capacity', 'enabled', 'generation', 'local_enabled', 'local_generation'}
    assert reservation['attempt_id'] not in json.dumps(status)
    assert runtime.reserve_cpu(people['owner'], project, **request)['state'] == 'reserved'
    # A fresh client/store sees persisted local disablement, not an automatic restart.
    restarted = Store(runtime.dsn, runtime.expected_database)
    assert restarted.cpu_control_status(people['owner'], project) == status


def test_control_read_is_scoped_coherent_and_does_not_write(controls, monkeypatch):
    admin, runtime, _, project, people, _ = controls
    request = setup(admin, people, project, actor='owner')
    admin.reserve_cpu(people['owner'], project, **request)
    other_project = 'other-' + uuid4().hex
    seed_api_authority(admin, other_project)
    other = setup(admin, people, other_project, actor='owner')
    admin.reserve_cpu(people['owner'], other_project, **other)
    # An unrelated pool contributes no capacity or held-unit data to this read.
    before = events(admin, project)
    original = runtime._connection
    changed = False

    class Interleaved:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, statement, parameters=None):
            nonlocal changed
            cursor = self.connection.execute(statement, parameters)
            if 'row_to_json(p)' in statement and not changed:
                changed = True
                with admin._connection() as writer:
                    writer.execute("UPDATE cpu_reservations SET state = 'cancelled' WHERE project_id = %s AND action_id = %s",
                                   (project, request['action_id']))
                    writer.execute('UPDATE cpu_pools SET capacity = 0, generation = generation + 1 WHERE project_id = %s', (project,))
            return cursor

    @contextmanager
    def interleaved():
        with original() as connection:
            yield Interleaved(connection)

    monkeypatch.setattr(runtime, '_connection', interleaved)
    status = runtime.cpu_control_status(people['owner'], project)
    assert changed and status['pool']['capacity'] == 1 and status['held_units'] == 1
    latest = runtime.cpu_control_status(people['owner'], project)
    assert latest['pool']['capacity'] == 0 and latest['held_units'] == 0
    assert events(admin, project) == before


@pytest.mark.parametrize('command,extra,expected', [
    ('cpu-control-get', [], ('get', 'project')),
    ('cpu-control-set', ['--capacity', '0', '--disable', '--expected-generation', '5', '--reason', '  reason  ', '--idempotency-key', 'stable'],
     ('central', 'project', 0, False, 5, '  reason  ', 'stable')),
    ('cpu-local-control-set', ['--enable', '--expected-generation', '6', '--reason', 'Local', '--idempotency-key', 'local-stable'],
     ('local', 'project', True, 6, 'Local', 'local-stable')),
])
def test_cli_forwards_exact_control_intent_and_ca(monkeypatch, capsys, tmp_path, command, extra, expected):
    from skybuild import __main__ as cli
    calls = []
    ca = tmp_path / 'ca.pem'

    class FakeClient:
        def __init__(self, url, token, *, ca_file):
            calls.append((url, token, ca_file))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def cpu_control_status(self, project):
            calls.append(('get', project))
            return {'project_id': project}

        def configure_cpu_pool(self, project, capacity, enabled, generation, *, reason, idempotency_key):
            calls.append(('central', project, capacity, enabled, generation, reason, idempotency_key))
            return {'project_id': project}

        def set_cpu_local_control(self, project, enabled, generation, *, reason, idempotency_key):
            calls.append(('local', project, enabled, generation, reason, idempotency_key))
            return {'project_id': project}

    monkeypatch.setattr(cli, 'Client', FakeClient)
    monkeypatch.setenv('SKYBUILD_API_URL', 'https://private.test')
    monkeypatch.setenv('SKYBUILD_TOKEN', 'SECRET')
    assert cli.main([command, 'project', '--ca-file', str(ca), *extra]) == 0
    assert calls == [('https://private.test', 'SECRET', ca), expected]
    output = capsys.readouterr()
    assert json.loads(output.out) == {'project_id': 'project'}
    assert 'SECRET' not in output.out + output.err


def test_client_local_and_read_use_scoped_paths_without_implicit_revisions():
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(200, json={'ok': True})

    with Client('https://private.test', 'token', transport=httpx.MockTransport(transport)) as client:
        client.cpu_control_status('project space')
        client.set_cpu_local_control('project space', False, 7, reason='  stop  ', idempotency_key='stable')
    assert requests[0].method == 'GET' and 'Idempotency-Key' not in requests[0].headers
    assert requests[0].url.raw_path == b'/api/v1/projects/project%20space/cpu-controls'
    assert requests[1].url.raw_path == b'/api/v1/projects/project%20space/cpu-controls/local'
    assert requests[1].headers['Idempotency-Key'] == 'stable'
    assert 'If-Match' not in requests[1].headers
    assert json.loads(requests[1].content) == {'enabled': False, 'expected_generation': 7, 'reason': '  stop  '}


def test_cli_mutation_requires_explicit_enable_or_disable(monkeypatch):
    from skybuild.__main__ import main
    with pytest.raises(SystemExit) as caught:
        main(['cpu-local-control-set', 'project', '--expected-generation', '1', '--reason', 'Missing intent'])
    assert caught.value.code == 2
