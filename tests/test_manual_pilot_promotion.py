"""Read-only promotion boundaries without touching a live controller."""
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import psycopg
import pytest
import skybuild
import skybuild.runtime_role as runtime_role

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import manual_pilot_controller as controller  # noqa: E402
import manual_pilot_provision as provisioner  # noqa: E402
import manual_pilot_tls as tls  # noqa: E402

assert Path(skybuild.__file__).resolve().parents[2] == Path(__file__).resolve().parents[1]


@pytest.fixture
def promotion(tmp_path, monkeypatch):
    state = tmp_path / 'private-state'
    provisioner.init_secrets(state)
    password = (state / 'secrets/runtime-password').read_text().strip()
    env = (f"SKYBUILD_DSN={provisioner._dsn(password, provisioner.DATABASE, host='db', port=5432)}\n"
           f"SKYBUILD_EXPECTED_DATABASE={provisioner.DATABASE}\n")
    provisioner._write_new(state / 'runtime.env', env, 0o600)
    (state / 'tls').mkdir(mode=0o700)
    (state / 'tls/ca.crt').write_bytes(b'retained fixture CA')
    current = {f'migrations/{i:03}_migration.sql': hashlib.sha256(str(i).encode()).hexdigest() for i in range(1, 12)}
    current.update({f'static/workbench.{suffix}': hashlib.sha256(suffix.encode()).hexdigest()
                    for suffix in ('css', 'html', 'js')})
    candidate = dict(current, **{'migrations/012_api_task_authority.sql': 'f' * 64})
    api_id, db_id, image = '1' * 64, '2' * 64, 'sha256:' + '3' * 64
    ip, hostname = '100.100.1.2', 'controller.tail.ts.net'
    ports = {'8000/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '8000'}, {'HostIp': ip, 'HostPort': '8443'}]}
    api = {'Id': api_id, 'Image': image,
           'Config': {'User': str(os.getuid()), 'Env': env.splitlines(),
                      'Cmd': ['python', '-m', 'skybuild', 'serve', '--host', '0.0.0.0', '--port', '8000',
                              '--ssl-certfile', '/run/skybuild-tls/server.crt', '--ssl-keyfile', '/run/skybuild-tls/server.key']},
           'HostConfig': {'PortBindings': ports, 'Memory': 512 * 1024**2, 'PidsLimit': 128,
                          'RestartPolicy': {'Name': 'unless-stopped'}, 'Privileged': False},
           'NetworkSettings': {'Ports': ports},
           'Mounts': [{'Source': str(state / 'tls' / name), 'Destination': '/run/skybuild-tls/' + name,
                       'RW': False} for name in ('server.crt', 'server.key')]}
    data = {'current': current, 'candidate': candidate, 'installed': current.copy(), 'api': api,
            'db': {'Id': db_id}, 'cluster': '123456', 'ready': {'status': 'ready'},
            'findings': [], 'controller_kwargs': {},
            'serve': {}, 'statements': [], 'calls': []}
    data['applied'] = [(i, current[f'migrations/{i:03}_migration.sql']) for i in range(1, 12)]
    arguments = dict(checkout=tmp_path / 'checkout', expected_sha='b' * 40,
                     published_ref='refs/heads/dev-002', current_sha='a' * 40,
                     state_dir=state, hostname=hostname, tailnet_ip=ip,
                     api_container=api_id, db_container=db_id, api_image=image, system_id='123456',
                     ca_pem_sha256=hashlib.sha256(b'retained fixture CA').hexdigest())

    def command(*args):
        data['calls'].append(args)
        if args[0] == 'git' and 'ls-remote' in args:
            return 'b' * 40 + '\trefs/heads/dev-002'
        if args[0] == 'git' and 'merge-base' in args:
            return ''
        if args[:2] == ('tailscale', 'serve'):
            return json.dumps(data['serve'])
        if args[:2] == ('docker', 'inspect'):
            return json.dumps([data['api'] if args[2] == 'skybuild-pilot-api' else data['db']])
        if args[:3] == ('docker', 'exec', 'skybuild-pilot-api'):
            return json.dumps(data['installed'])
        if args[0] == 'curl':
            return json.dumps(data['ready'])
        raise AssertionError(args)

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, statement):
            data['statements'].append(statement)
            self.statement = statement
            return self

        def fetchone(self):
            return (provisioner.DATABASE,) if 'current_database' in self.statement else (data['cluster'],)

        def fetchall(self):
            return data['applied']

    monkeypatch.setattr(tls, 'command', command)
    monkeypatch.setattr(tls, 'controller', lambda *args, **kwargs: data['controller_kwargs'].update(kwargs))
    monkeypatch.setattr(tls, 'check', lambda *args: {'ca_sha256': 'public-ca-fingerprint', 'service_changes': False})
    monkeypatch.setattr(controller, '_source_manifest', lambda path, sha: data['current' if sha == 'a' * 40 else 'candidate'])
    monkeypatch.setattr(controller, '_available_gib', lambda: 20)
    monkeypatch.setattr(controller.shutil, 'disk_usage', lambda path: SimpleNamespace(free=20 * 1024**3))
    monkeypatch.setattr(controller.os, 'getpriority', lambda *args: 10)
    monkeypatch.setattr(provisioner, '_dedicated_container', lambda path: data['cluster'])
    monkeypatch.setattr(psycopg, 'connect', lambda *args, **kwargs: Connection())
    monkeypatch.setattr(runtime_role, 'audit_runtime_role', lambda *args: {'ok': not data['findings'], 'findings': data['findings']})
    return arguments, data


def test_promotion_checks_are_read_only_and_refuse_binary_rollback(promotion):
    arguments, data = promotion
    report = controller.promotion_preflight(**arguments)
    assert report['ready_for_operator_promotion'] is True
    assert report['no_changes_made'] is True
    assert report['binary_rollback_after_migration'] is False
    assert report['candidate_role_audit_required'] is True
    assert (report['current_schema'], report['candidate_schema']) == (11, 12)
    assert data['controller_kwargs']['expected_api_image'] == arguments['api_image']
    assert all(statement.startswith(('SELECT ', 'SET TRANSACTION ')) for statement in data['statements'])
    assert not any(any(word in args for word in ('stop', 'start', 'build', 'up', 'restart', 'migrate')) for args in data['calls'])
    assert 'password' not in json.dumps(report)


@pytest.mark.parametrize('boundary', [
    'api-id', 'db-id', 'image', 'installed-source', 'changed-prefix', 'extra-migration',
    'applied-digest', 'already-migrated', 'cluster', 'runtime-env', 'admin-env',
    'tls-user', 'public-port', 'writable-key', 'privileged', 'memory-limit',
    'old-readiness', 'role-excess', 'serve-route', 'changed-ca',
    'changed-static', 'missing-static', 'unexpected-package-file',
])
def test_promotion_refuses_changed_boundary(promotion, boundary):
    arguments, data = promotion
    if boundary == 'api-id': data['api']['Id'] = '4' * 64
    elif boundary == 'db-id': data['db']['Id'] = '4' * 64
    elif boundary == 'image': data['api']['Image'] = 'sha256:' + '4' * 64
    elif boundary == 'installed-source': data['installed']['unexpected.py'] = 'x'
    elif boundary == 'changed-prefix': data['candidate']['migrations/001_migration.sql'] = 'changed'
    elif boundary == 'extra-migration': data['candidate']['migrations/012_other.sql'] = 'changed'
    elif boundary == 'applied-digest': data['applied'][0] = (1, 'changed')
    elif boundary == 'already-migrated': data['applied'].append((12, 'f' * 64))
    elif boundary == 'cluster': data['cluster'] = '999999'
    elif boundary == 'runtime-env': (arguments['state_dir'] / 'runtime.env').write_text('SKYBUILD_DSN=foreign')
    elif boundary == 'admin-env': data['api']['Config']['Env'].append('SKYBUILD_ROLE_ADMIN_DSN=secret')
    elif boundary == 'tls-user': data['api']['Config']['User'] = '0' if os.getuid() else '999'
    elif boundary == 'public-port': data['api']['NetworkSettings']['Ports']['8000/tcp'][0]['HostIp'] = '0.0.0.0'
    elif boundary == 'writable-key': data['api']['Mounts'][0]['RW'] = True
    elif boundary == 'privileged': data['api']['HostConfig']['Privileged'] = True
    elif boundary == 'memory-limit': data['api']['HostConfig']['Memory'] = 0
    elif boundary == 'old-readiness': data['ready']['status'] = 'unavailable'
    elif boundary == 'role-excess': data['findings'].append('excess skybuild.principals UPDATE')
    elif boundary == 'serve-route': data['serve'] = {'TCP': {'443': {}}}
    elif boundary == 'changed-ca': (arguments['state_dir'] / 'tls/ca.crt').write_bytes(b'replacement CA')
    elif boundary == 'changed-static': data['installed']['static/workbench.js'] = 'changed'
    elif boundary == 'missing-static': del data['installed']['static/workbench.html']
    elif boundary == 'unexpected-package-file': data['installed']['unexpected.txt'] = 'changed'
    with pytest.raises(ValueError):
        controller.promotion_preflight(**arguments)


@pytest.mark.parametrize('boundary', ['memory', 'nice', 'missing-identity'])
def test_promotion_resource_and_identity_guards(promotion, monkeypatch, boundary):
    arguments, _ = promotion
    if boundary == 'memory': monkeypatch.setattr(controller, '_available_gib', lambda: 9)
    elif boundary == 'nice': monkeypatch.setattr(controller.os, 'getpriority', lambda *args: 0)
    else: arguments['api_container'] = 'short-id'
    with pytest.raises(ValueError):
        controller.promotion_preflight(**arguments)


def test_promotion_cli_hides_arbitrary_driver_errors(promotion, monkeypatch, capsys):
    arguments, _ = promotion
    monkeypatch.setattr(controller, 'promotion_preflight', lambda *args, **kwargs: (_ for _ in ()).throw(psycopg.OperationalError('SECRET DSN')))
    flags = ['--promotion', '--checkout', str(arguments['checkout']), '--expected-sha', arguments['expected_sha'],
             '--published-ref', arguments['published_ref'], '--current-sha', arguments['current_sha'],
             '--state-dir', str(arguments['state_dir']), '--hostname', arguments['hostname'],
             '--tailnet-ip', arguments['tailnet_ip'], '--api-container-id', arguments['api_container'],
             '--db-container-id', arguments['db_container'], '--api-image-id', arguments['api_image'],
             '--database-system-id', arguments['system_id'], '--ca-pem-sha256', arguments['ca_pem_sha256']]
    assert controller.main(flags) == 2
    output = capsys.readouterr().out
    assert 'SECRET' not in output
    assert json.loads(output)['no_changes_made'] is True


def test_source_manifest_matches_exact_git_blob_bytes():
    root = Path(__file__).resolve().parents[1]
    revision = '7d40df9fa7b26035736ffa613b5c5dad548269f5'
    manifest = controller._source_manifest(root, revision)
    result = controller._command('git', '-C', str(root), 'show', revision + ':src/skybuild/store.py')
    assert result.returncode == 0
    assert manifest['store.py'] == hashlib.sha256(result.stdout.encode()).hexdigest()
    assert {f'static/workbench.{suffix}' for suffix in ('css', 'html', 'js')} <= manifest.keys()
    assert len([name for name in manifest if name.startswith('migrations/')]) == 10


def test_installed_probe_includes_assets_and_unexpected_files(promotion, tmp_path):
    import subprocess
    import sys
    arguments, data = promotion
    controller.promotion_preflight(**arguments)
    probe = next(args[-1] for args in data['calls'] if args[:3] == ('docker', 'exec', 'skybuild-pilot-api'))
    package = tmp_path / 'probe/skybuild'
    (package / 'static').mkdir(parents=True)
    (package / '__init__.py').write_text('')
    asset = package / 'static/workbench.js'
    asset.write_text('original')
    def inspect():
        return json.loads(subprocess.check_output([sys.executable, '-c', probe],
                          env={**os.environ, 'PYTHONPATH': str(package.parent)}, text=True))
    first = inspect()
    assert first['static/workbench.js'] == hashlib.sha256(b'original').hexdigest()
    assert not any('__pycache__' in name for name in first)
    asset.write_text('changed')
    assert inspect()['static/workbench.js'] != first['static/workbench.js']
    asset.unlink()
    (package / 'unexpected.txt').write_text('foreign')
    (package / '__pycache__/unexpected.txt').write_text('foreign cache-directory file')
    final = inspect()
    assert 'static/workbench.js' not in final and 'unexpected.txt' in final
    assert '__pycache__/unexpected.txt' in final


@pytest.mark.parametrize('boundary', ['foreign-owner', 'collision', 'success'])
def test_runbook_replacement_never_mutates_a_foreign_container(promotion, monkeypatch, boundary):
    arguments, data = promotion
    root = Path(__file__).resolve().parents[1]
    report = dict(api_container=arguments['api_container'], api_image=arguments['api_image'],
                  database_container=arguments['db_container'])
    report_path = arguments['state_dir'] / 'promotion-report'
    provisioner._write_new(report_path, json.dumps(report), 0o600)
    for name, value in {'SKYBUILD_PILOT_STATE': str(arguments['state_dir']),
                        'SKYBUILD_PROMOTION_REPORT': str(report_path),
                        'SKYBUILD_PROMOTION_IMAGE_ID': 'sha256:' + '5' * 64,
                        'SKYBUILD_PILOT_TAILNET_IP': arguments['tailnet_ip']}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.chdir(root)
    api = data['api']
    api['State'] = {'Running': False}
    api['NetworkSettings']['Networks'] = {'existing': {'NetworkID': '6' * 64}}
    api['Config']['Labels'] = {'com.docker.compose.project': 'skybuild-pilot',
                              'com.docker.compose.service': 'api',
                              'com.docker.compose.project.working_dir': str(root / 'ops/manual-pilot'),
                              'com.docker.compose.project.config_files': 'retained-files'}
    calls = []
    def command(*args):
        calls.append(args)
        if args[:2] == ('docker', 'inspect'):
            if args[2] == report['api_container']: return json.dumps([api])
            if args[2] == 'skybuild-pilot-api':
                return json.dumps([{**api, 'Id': '9' * 64} if boundary == 'foreign-owner' else api])
            if args[2] == report['database_container']:
                return json.dumps([{'Id': report['database_container'], 'State': {'Running': True},
                                    'NetworkSettings': {'Networks': api['NetworkSettings']['Networks']}}])
            if args[2] == '7' * 64:
                return json.dumps([{'Id': '7' * 64, 'Image': 'sha256:' + '5' * 64}])
        if args[:2] == ('docker', 'rm'): return args[2]
        if args[:2] == ('docker', 'create'):
            if boundary == 'collision': raise RuntimeError('name already owned')
            return '7' * 64
        if args[:2] == ('docker', 'start'): return args[2]
        raise AssertionError(args)
    monkeypatch.setattr(tls, 'command', command)
    document = (root / 'docs/design/implementation/manual_pilot_promotion.md').read_text()
    block = document.split("<<'PY'\n")[3].split('\nPY\n', 1)[0]
    if boundary == 'success': exec(compile(block, '<operator-runbook>', 'exec'), {})
    else:
        with pytest.raises(SystemExit): exec(compile(block, '<operator-runbook>', 'exec'), {})
    mutations = [args for args in calls if args[1] in ('rm', 'create', 'start')]
    if boundary == 'foreign-owner': assert not mutations
    else:
        assert mutations[0] == ('docker', 'rm', report['api_container'])
        assert mutations[1][:4] == ('docker', 'create', '--pull', 'never')
        if boundary == 'success': assert mutations[2] == ('docker', 'start', '7' * 64)
        else: assert not any(args[1] == 'start' for args in mutations)
    assert 'docker stop --time 30 "$CURRENT_API_CONTAINER"' in document
    assert 'docker compose ' not in document


def test_binding_order_does_not_change_owned_addresses(promotion):
    arguments, data = promotion
    data['api']['NetworkSettings']['Ports']['8000/tcp'].reverse()
    assert controller.promotion_preflight(**arguments)['ready_for_operator_promotion'] is True


def test_schema_010_role_audit_and_atomic_candidate_requalification(monkeypatch):
    from uuid import uuid4
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    from fastapi.testclient import TestClient
    from skybuild.api import create_app
    from skybuild.store import Store

    base = os.environ.get('SKYBUILD_HTTP_TEST_DSN')
    if not base:
        pytest.skip('Requires parent-owned disposable PostgreSQL gate')
    database = conninfo_to_dict(base).get('dbname', '')
    if not database.startswith('skybuild_') or not database.endswith('_test'):
        pytest.fail('Promotion rehearsal requires an explicitly disposable database')
    target, role, role_password = ('skybuild_promotion_' + uuid4().hex + '_test',
                                  'runtime_' + uuid4().hex, uuid4().hex + uuid4().hex)
    dsn = make_conninfo(base, dbname=target)
    runtime_dsn = make_conninfo(base, dbname=target, user=role, password=role_password)
    migrations = sorted((Path(skybuild.__file__).parent / 'migrations').glob('*.sql'))
    old = [path for path in migrations if int(path.name.split('_', 1)[0]) <= 10]
    expansions = [path for path in migrations if int(path.name.split('_', 1)[0]) > 10]
    authority = next(path for path in migrations if path.name == '012_api_task_authority.sql')
    with psycopg.connect(base, autocommit=True) as cluster:
        cluster.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(target)))
        cluster.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {}').format(sql.Identifier(role), sql.Literal(role_password)))
    try:
        store = Store(dsn, target)
        with store._connection() as connection:
            connection.execute('CREATE SCHEMA skybuild')
            connection.execute('CREATE TABLE schema_migrations (version integer PRIMARY KEY, digest text NOT NULL)')
            for path in old:
                connection.execute(path.read_text())
                connection.execute('INSERT INTO schema_migrations VALUES (%s, %s)',
                                   (int(path.name.split('_', 1)[0]), hashlib.sha256(path.read_bytes()).hexdigest()))
        # The schema010 policy differs only by its two absent simulator tables.
        fake_tables = {'cpu_fake_dispatches', 'cpu_fake_receipts'}
        with monkeypatch.context() as patch:
            patch.setattr(runtime_role, 'TABLES', runtime_role.TABLES - fake_tables)
            patch.setattr(runtime_role, 'MUTABLE', runtime_role.MUTABLE - fake_tables)
            with psycopg.connect(dsn) as connection:
                assert runtime_role.provision_runtime_role(connection, target, role)['ok'] is True
        with psycopg.connect(dsn) as connection:
            assert runtime_role.audit_runtime_role(connection, target, role)['findings'] == [
                'missing table: cpu_fake_dispatches', 'missing table: cpu_fake_receipts']

        def upgrade(connection):
            connection.execute('SET LOCAL search_path TO skybuild, pg_catalog')
            for expansion in expansions:
                connection.execute(expansion.read_text())
                connection.execute('INSERT INTO schema_migrations VALUES (%s, %s)',
                                   (int(expansion.name.split('_', 1)[0]),
                                    hashlib.sha256(expansion.read_bytes()).hexdigest()))
            assert runtime_role.provision_runtime_role(connection, target, role)['ok'] is True
            connection.execute(authority.read_text())
            connection.execute('INSERT INTO schema_migrations VALUES (12, %s)',
                               (hashlib.sha256(authority.read_bytes()).hexdigest(),))
            assert runtime_role.provision_runtime_role(connection, target, role)['ok'] is True

        class Accepted010Store(Store):
            def readiness(self):
                with self._connection() as connection:
                    versions = connection.execute(
                        'SELECT version, digest FROM schema_migrations ORDER BY version').fetchall()
                expected = [
                    {'version': int(path.name.split('_', 1)[0]),
                     'digest': hashlib.sha256(path.read_bytes()).hexdigest()}
                    for path in old
                ]
                if versions != expected:
                    raise RuntimeError('Accepted schema-010 controller found incompatible database')
                return {'ready': True, 'schema_version': 10}

        owner, worker = 'owner-' + uuid4().hex, 'worker-' + uuid4().hex
        owner_token, worker_token = uuid4().hex + uuid4().hex, uuid4().hex + uuid4().hex
        project = 'promotion-' + uuid4().hex
        registry = Store(dsn, target)
        registry.provision_principal(owner, owner_token, is_admin=True)
        registry.provision_principal(worker, worker_token, grants={project: [
            'tasks:read', 'tasks:write', 'cord:send', 'cord:read', 'cord:handle']})

        def headers(token, key=None):
            return {'Authorization': 'Bearer ' + token, 'Idempotency-Key': key or uuid4().hex}

        def preserved_state(client):
            api = f'/api/v1/projects/{project}'
            assert client.get('/health/ready').json() == {'status': 'ready'}
            task_history = client.get(api + '/tasks/promotion-task/history',
                                      headers=headers(owner_token))
            assert task_history.status_code == 200, task_history.text
            assert len(task_history.json()) == 1
            inbox = client.get(api + '/cord/inbox', headers=headers(worker_token))
            assert inbox.status_code == 200, inbox.text
            assert [message['subject'] for message in inbox.json()] == ['Pinned assignment']
            return task_history.json(), inbox.json()

        accepted = Accepted010Store(runtime_dsn, target)
        with TestClient(create_app(accepted)) as client:
            assert client.get('/health/ready').json() == {'status': 'ready'}
            api = f'/api/v1/projects/{project}'
            task = client.post(api + '/tasks', headers=headers(owner_token, 'create-task'), json={
                'task_id': 'promotion-task', 'title': 'Retain task history', 'description': 'Schema rehearsal'})
            assert task.status_code == 201, task.text
            message = client.post(api + '/cord/messages', headers=headers(owner_token, 'send-cord'), json={
                'recipient': worker, 'subject': 'Pinned assignment', 'body': 'Keep this record'})
            assert message.status_code == 201, message.text

            # Candidate preparation is represented by bounded migration/source hashing only.
            candidate_schema_digest = hashlib.sha256(
                b''.join(path.read_bytes() for path in expansions)).hexdigest()
            assert len(candidate_schema_digest) == 64
            retained_records = preserved_state(client)

            with pytest.raises(RuntimeError, match='Qualification boundary failure'):
                with psycopg.connect(dsn) as connection:
                    upgrade(connection)
                    raise RuntimeError('Qualification boundary failure')
            assert client.get('/health/ready').json() == {'status': 'ready'}
            assert preserved_state(client) == retained_records

        with psycopg.connect(dsn) as connection:
            assert connection.execute('SELECT max(version) FROM skybuild.schema_migrations').fetchone()[0] == 10
            assert connection.execute("SELECT to_regclass('skybuild.cpu_fake_dispatches')").fetchone()[0] is None
            assert runtime_role.audit_runtime_role(connection, target, role)['findings'] == [
                'missing table: cpu_fake_dispatches', 'missing table: cpu_fake_receipts']
        with psycopg.connect(dsn) as connection:
            upgrade(connection)
        assert store.readiness() == {'ready': True, 'schema_version': 12}
        with psycopg.connect(dsn) as connection:
            assert runtime_role.audit_runtime_role(connection, target, role)['ok'] is True

        with pytest.raises(RuntimeError, match='incompatible database'):
            accepted.readiness()

        class FailedCandidateStore(Store):
            def readiness(self):
                raise RuntimeError('Injected candidate readiness failure')

        with TestClient(create_app(FailedCandidateStore(runtime_dsn, target))) as failed_candidate:
            assert failed_candidate.get('/health/ready').status_code == 503

        recovered_candidate = Store(runtime_dsn, target)
        with TestClient(create_app(recovered_candidate)) as client:
            assert preserved_state(client) == retained_records
    finally:
        with psycopg.connect(base, autocommit=True) as cluster:
            cluster.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(target)))
            cluster.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))
