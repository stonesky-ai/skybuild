"""Bounded dispatcher collection with scratch Git and synthetic REST only."""
from datetime import datetime, timezone
import json
import subprocess

import pytest

from skybuild.manual_result import ResultError, receive_result
from skybuild.manual_cord import ManualCordError
from test_manual_assignment import pinned  # noqa: F401


@pytest.fixture
def collection(pinned, tmp_path):
    repo, assignment = pinned
    subprocess.run(['git', '-C', str(repo), 'checkout', '-qb', assignment['branch']], check=True)
    owned = repo / assignment['owned_paths'][0]
    owned.parent.mkdir(parents=True)
    owned.write_text('synthetic CPU-only fixture\n')
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repo), 'commit', '-qm', 'Worker change'], check=True)
    head = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD']).decode().strip()
    result = {'schema': 'manual-work-v1', 'assignment_id': assignment['assignment_id'],
              'phase': 'ready-for-review', 'branch': assignment['branch'], 'head_sha': head,
              'checks': ['synthetic fixture check'], 'changed_paths': assignment['owned_paths'],
              'risks': [], 'next_action': 'Independent review of pinned head'}
    message = {'message_id': 'result-001', 'sender': assignment['worker'],
               'recipient': assignment['dispatcher'], 'category': 'manual-work', 'body': json.dumps(result)}
    now = [1000.0]
    actions, pages, git_calls = [], [], []
    destination = tmp_path / 'collected.json'

    class FakeClient:
        messages = [message]
        lose_reply = False

        def whoami(self):
            return {'principal_id': assignment['dispatcher'], 'is_admin': False,
                    'grants': {'skybuild': ['cord:send', 'cord:read', 'cord:handle']}}

        def inbox(self, project, *, limit, offset):
            pages.append(offset)
            return self.messages[offset:offset + limit]

        def message_action(self, project, message_id, action, *, idempotency_key):
            assert json.loads(destination.read_text())['result'] == json.loads(message['body'])
            actions.append((project, message_id, action, idempotency_key))
            if self.lose_reply:
                self.lose_reply = False
                raise TimeoutError('SECRET transport diagnostic')
            return {'message_id': message_id, 'delivered_at': 'synthetic receipt'}

    client = FakeClient()

    def git(repo, *args, timeout):
        assert 0 < timeout <= 10
        git_calls.append(args)
        if args[0] == 'ls-remote':
            return (head + '\trefs/heads/' + assignment['branch']).encode()
        return subprocess.check_output(['git', *args], cwd=repo, timeout=timeout)

    kwargs = dict(assignment=assignment, assignment_id=assignment['assignment_id'],
                  task_id=assignment['task_id'], worker=assignment['worker'],
                  dispatcher=assignment['dispatcher'], base_sha=assignment['base_sha'],
                  message_id=message['message_id'], destination=destination,
                  approval_until=datetime.fromtimestamp(2000, timezone.utc).isoformat(),
                  clock=lambda: now[0], monotonic=lambda: now[0], git_runner=git)
    return client, repo, kwargs, result, message, actions, pages, now, git_calls


def collect(c):
    client, repo, kwargs, *_ = c
    return receive_result(client, 'skybuild', repo, **kwargs)


def test_lost_receipt_reply_replays_same_persisted_result_without_handling(collection):
    client, _, kwargs, _, _, actions, *_ = collection
    client.lose_reply = True
    with pytest.raises(TimeoutError):
        collect(collection)
    original = kwargs['destination'].read_bytes()
    result = collect(collection)
    assert kwargs['destination'].read_bytes() == original
    assert kwargs['destination'].stat().st_mode & 0o777 == 0o600
    assert actions[0] == actions[1] and actions[0][2] == 'receipt'
    assert result['review_required'] is True and result['authority'] == 'markdown'
    assert 'checks' not in result and 'risks' not in result


@pytest.mark.parametrize('change', ['sender', 'recipient', 'assignment', 'phase', 'scope', 'head', 'paths', 'pin'])
def test_forged_or_unowned_results_never_persist_or_acknowledge(collection, change):
    _, _, kwargs, result, message, actions, *_ = collection
    if change in {'sender', 'recipient'}:
        message[change] = 'impostor'
    elif change == 'pin':
        kwargs['task_id'] = 'SKYBUILD-OTHER'
    else:
        key, value = {'assignment': ('assignment_id', 'other'), 'phase': ('phase', 'done'),
                      'scope': ('changed_paths', ['src/skybuild/store.py']),
                      'head': ('head_sha', '0' * 40),
                      'paths': ('changed_paths', ['../secret'])}[change]
        result[key] = value
        message['body'] = json.dumps(result)
    with pytest.raises(ValueError):
        collect(collection)
    assert not kwargs['destination'].exists() and actions == []


def test_conflicting_body_cannot_overwrite_saved_evidence(collection):
    _, _, kwargs, result, message, actions, *_ = collection
    collect(collection)
    original = kwargs['destination'].read_bytes()
    result['next_action'] = 'Different report with same message identity'
    message['body'] = json.dumps(result)
    with pytest.raises(ManualCordError):
        collect(collection)
    assert kwargs['destination'].read_bytes() == original and len(actions) == 1


def test_pagination_is_bounded_and_can_find_second_page(collection):
    client, _, kwargs, _, message, actions, pages, *_ = collection
    client.messages = [{'message_id': 'unrelated'}] * 100 + [message]
    collect(collection)
    assert pages == [0, 100] and len(actions) == 1
    kwargs['destination'].unlink()
    actions.clear()
    pages.clear()
    kwargs['max_pages'] = 1
    with pytest.raises(ResultError, match='bounded inbox'):
        collect(collection)
    assert pages == [0] and actions == []


def test_expired_approval_and_cutoff_after_persistence_never_ack(collection, monkeypatch):
    import skybuild.manual_result as module
    _, _, kwargs, _, _, actions, _, now, git_calls = collection
    now[0] = 2001
    with pytest.raises(ResultError, match='deadline'):
        collect(collection)
    assert git_calls == [] and actions == []
    now[0] = 1000
    write = module._private_write

    def expire_after_save(*args):
        write(*args)
        now[0] = 2001

    monkeypatch.setattr(module, '_private_write', expire_after_save)
    with pytest.raises(ResultError, match='deadline'):
        collect(collection)
    assert kwargs['destination'].exists() and actions == []


def test_monotonic_budget_survives_wall_clock_rollback(collection):
    client, _, kwargs, *_ = collection
    wall, mono = [1000], [1000]
    kwargs.update(clock=lambda: wall[0], monotonic=lambda: mono[0], duration=1)
    original = client.inbox

    def elapsed(*args, **kw):
        wall[0] -= 100
        mono[0] += 2
        return original(*args, **kw)

    client.inbox = elapsed
    with pytest.raises(ResultError, match='deadline'):
        collect(collection)
    assert not kwargs['destination'].exists()


def test_unpushed_head_and_git_diff_mismatch_stop_receipt(collection):
    _, _, kwargs, _, _, actions, *_ = collection
    original = kwargs['git_runner']

    def foreign(repo, *args, timeout):
        if args[0] == 'ls-remote':
            return ('0' * 40 + '\trefs/heads/other').encode()
        return original(repo, *args, timeout=timeout)

    kwargs['git_runner'] = foreign
    with pytest.raises(ResultError, match='pushed'):
        collect(collection)
    assert actions == [] and not kwargs['destination'].exists()
    def changed(repo, *args, timeout):
        return b'other.py\x00' if args[0] == 'diff' else original(repo, *args, timeout=timeout)
    kwargs['git_runner'] = changed
    with pytest.raises(ResultError, match='Git evidence'):
        collect(collection)
    assert actions == [] and not kwargs['destination'].exists()


def test_dispatcher_identity_and_failed_persistence_stop_receipt(collection, monkeypatch):
    import skybuild.manual_result as module
    client, _, kwargs, _, _, actions, *_ = collection
    identity = client.whoami
    client.whoami = lambda: {**identity(), 'is_admin': True}
    with pytest.raises(ResultError, match='credential'):
        collect(collection)
    client.whoami = identity
    def failed(*args):
        raise OSError('SECRET filesystem diagnostic')
    monkeypatch.setattr(module, '_private_write', failed)
    with pytest.raises(OSError):
        collect(collection)
    assert actions == [] and not kwargs['destination'].exists()


def test_cli_suppresses_secret_diagnostics_and_expiry_precedes_dns(collection, monkeypatch, capsys):
    import skybuild.manual_result as module
    client, repo, kwargs, *_ = collection
    assignment_path = repo.parent / 'assignment.json'
    assignment_path.write_text(json.dumps(kwargs['assignment']))
    argv = ['--url', 'https://private.ts.net', '--project', 'skybuild',
            '--worker', kwargs['worker'], '--dispatcher', kwargs['dispatcher'],
            '--assignment-id', kwargs['assignment_id'], '--task-id', kwargs['task_id'],
            '--base-sha', kwargs['base_sha'], '--message-id', kwargs['message_id'],
            '--approval-until', '2999-01-01T00:00:00+00:00', '--token-file', 'unused',
            '--checkout', str(repo), '--assignment', str(assignment_path),
            '--destination', str(kwargs['destination'])]
    monkeypatch.setattr(module, '_private_endpoint', lambda url, resolver: url)
    monkeypatch.setattr(module, '_token_from_file', lambda path: 'SECRET-token')
    class Context:
        def __init__(self, *args, **kw):
            assert kw['retries'] == 0 and kw['trust_env'] is False
        def __enter__(self):
            return client
        def __exit__(self, *args):
            pass
    monkeypatch.setattr(module, 'Client', Context)
    def failed(*args, **kw):
        raise ResultError('SECRET transport diagnostic')
    monkeypatch.setattr(module, 'receive_result', failed)
    assert module.main(argv) == 2
    assert 'SECRET' not in capsys.readouterr().err
    monkeypatch.setattr(module, '_private_endpoint', lambda *args: pytest.fail('Expired collector reached DNS'))
    argv[argv.index('--approval-until') + 1] = '2000-01-01T00:00:00+00:00'
    assert module.main(argv) == 2


def test_real_client_requires_disabled_automatic_retries(collection):
    from skybuild.client import Client
    import httpx
    _, repo, kwargs, *_ = collection
    with Client('https://private.test', 'synthetic-token', retries=1,
                transport=httpx.MockTransport(lambda request: pytest.fail('Unexpected REST'))) as client:
        with pytest.raises(ResultError, match='disable automatic retries'):
            receive_result(client, 'skybuild', repo, **kwargs)


def test_progressing_http_body_cannot_outlive_total_deadline(collection):
    """Read inactivity timeouts alone cannot bound a continuously slow body."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    import time
    from skybuild.client import Client

    _, repo, kwargs, *_ = collection
    body = json.dumps({'principal_id': kwargs['dispatcher'], 'is_admin': False,
                       'grants': {'skybuild': ['cord:read', 'cord:send', 'cord:handle']}}).encode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            try:
                for offset in range(0, len(body), 4):
                    self.wfile.write(body[offset:offset + 4])
                    self.wfile.flush()
                    time.sleep(0.08)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    kwargs.update(clock=time.time, monotonic=time.monotonic, duration=1,
                  approval_until='2999-01-01T00:00:00+00:00')
    try:
        with Client(f'http://127.0.0.1:{server.server_port}', 'synthetic-token', retries=0,
                    timeout=1, trust_env=False) as client:
            started = time.monotonic()
            with pytest.raises(ResultError, match='deadline'):
                receive_result(client, 'skybuild', repo, **kwargs)
            elapsed = time.monotonic() - started
        assert 0.8 <= elapsed < 1.6
        assert not kwargs['destination'].exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_cli_blocked_resolver_is_interrupted_before_credentials(collection, monkeypatch, capsys):
    import time
    import skybuild.manual_result as module
    _, repo, kwargs, *_ = collection
    argv = ['--url', 'https://private.ts.net', '--project', 'skybuild',
            '--worker', kwargs['worker'], '--dispatcher', kwargs['dispatcher'],
            '--assignment-id', kwargs['assignment_id'], '--task-id', kwargs['task_id'],
            '--base-sha', kwargs['base_sha'], '--message-id', kwargs['message_id'],
            '--approval-until', '2999-01-01T00:00:00+00:00', '--token-file', 'unused',
            '--checkout', str(repo), '--assignment', 'unused', '--duration', '1',
            '--destination', str(kwargs['destination'])]

    def blocked(host):
        time.sleep(3)
        return ['100.100.100.100']

    monkeypatch.setattr(module, '_resolved_addresses', blocked)
    monkeypatch.setattr(module, '_token_from_file', lambda *args: pytest.fail('Blocked DNS reached credentials'))
    started = time.monotonic()
    assert module.main(argv) == 2
    assert 0.8 <= time.monotonic() - started < 1.6
    assert 'failed' in capsys.readouterr().err
    assert not kwargs['destination'].exists()


def test_interrupted_receipt_preserves_evidence_for_valid_restart(collection):
    import time
    client, _, kwargs, _, _, actions, *_ = collection
    original = client.message_action

    def accepted_then_blocked(*args, **options):
        original(*args, **options)
        time.sleep(3)

    client.message_action = accepted_then_blocked
    kwargs['duration'] = 1
    started = time.monotonic()
    with pytest.raises(ResultError, match='deadline'):
        collect(collection)
    assert time.monotonic() - started < 1.6
    saved = kwargs['destination'].read_bytes()
    client.message_action = original
    assert collect(collection)['receipted'] is True
    assert kwargs['destination'].read_bytes() == saved
    assert actions[0] == actions[1] and actions[0][2] == 'receipt'


def test_non_main_thread_refuses_collection_before_git(collection):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=1) as executor:
        with pytest.raises(ResultError, match='main thread'):
            executor.submit(collect, collection).result(timeout=2)
    assert collection[-1] == []
