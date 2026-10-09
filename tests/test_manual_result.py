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
    assert module._main(argv) == 2
    assert 'SECRET' not in capsys.readouterr().err
    monkeypatch.setattr(module, '_private_endpoint', lambda *args: pytest.fail('Expired collector reached DNS'))
    argv[argv.index('--approval-until') + 1] = '2000-01-01T00:00:00+00:00'
    assert module._main(argv) == 2


def test_real_client_requires_disabled_automatic_retries(collection):
    from skybuild.client import Client
    import httpx
    _, repo, kwargs, *_ = collection
    with Client('https://private.test', 'synthetic-token', retries=1,
                transport=httpx.MockTransport(lambda request: pytest.fail('Unexpected REST'))) as client:
        with pytest.raises(ResultError, match='disable automatic retries'):
            receive_result(client, 'skybuild', repo, **kwargs)



def cli_args(collection):
    _, repo, kwargs, *_ = collection
    assignment = repo.parent / 'assignment.json'
    assignment.write_text(json.dumps(kwargs['assignment']))
    token = repo.parent / 'token'
    token.write_text('synthetic-token-xxxxxxxxxxxxxxxxxxxxxxxx')
    token.chmod(0o600)
    values = {'url': 'https://private.ts.net', 'project': 'skybuild',
              'worker': kwargs['worker'], 'dispatcher': kwargs['dispatcher'],
              'assignment-id': kwargs['assignment_id'], 'task-id': kwargs['task_id'],
              'base-sha': kwargs['base_sha'], 'message-id': kwargs['message_id'],
              'approval-until': '2999-01-01T00:00:00+00:00', 'token-file': token,
              'checkout': repo, 'assignment': assignment,
              'destination': kwargs['destination'], 'duration': 1}
    return [str(part) for pair in values.items() for part in ('--' + pair[0], pair[1])]


def test_cli_supervisor_bounds_real_libc_dns(collection, tmp_path, monkeypatch, capsys):
    import socket
    import time
    import skybuild.manual_result as module
    source = tmp_path / 'resolver.c'
    source.write_text("""
#include <resolv.h>
#include <arpa/inet.h>
int configure_resolver(unsigned short port) {
    res_state r = __res_state();
    if (res_ninit(r)) return -1;
    r->nscount = 1;
    r->nsaddr_list[0].sin_family = AF_INET;
    r->nsaddr_list[0].sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    r->nsaddr_list[0].sin_port = htons(port);
    r->retrans = 3; r->retry = 1;
    r->options |= RES_INIT;
    r->options &= ~(RES_ROTATE | RES_USEVC);
    return 0;
}
""")
    library = tmp_path / 'resolver.so'
    subprocess.run(['cc', '-shared', '-fPIC', str(source), '-o', str(library), '-lresolv'], check=True)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as resolver:
        resolver.bind(('127.0.0.1', 0))
        resolver.settimeout(0.2)
        prefix = ("import ctypes; lib=ctypes.CDLL(" + repr(str(library)) +
                  "); assert lib.configure_resolver(" + str(resolver.getsockname()[1]) + ") == 0; ")
        monkeypatch.setattr(module, '_CHILD_CODE', prefix + module._CHILD_CODE)
        started = time.monotonic()
        assert module.main(cli_args(collection)) == 2
        assert time.monotonic() - started < 1.8
        assert resolver.recvfrom(4096)[0]  # Actual libc resolver reached the dropped UDP reply.
    assert not collection[2]['destination'].exists()
    assert 'synthetic-token' not in capsys.readouterr().err


def test_cli_supervisor_bounds_progressing_http_body(collection, monkeypatch):
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import skybuild.manual_result as module
    reached = threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            reached.set()
            self.send_response(200)
            self.send_header('Content-Length', '100')
            self.end_headers()
            try:
                for _ in range(100):
                    self.wfile.write(b' ')
                    self.wfile.flush()
                    time.sleep(0.08)
            except (BrokenPipeError, ConnectionResetError):
                pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # Exercise real CLI credential/client setup and real progressing HTTP.
        code = module._CHILD_CODE.replace('from skybuild.manual_result import _main;',
            'import skybuild.manual_result as m; '
            'm._private_endpoint=lambda url,resolver:url; '
            'm.receive_result=lambda client,*args,**kwargs:client.whoami(); '
            'from skybuild.manual_result import _main;')
        monkeypatch.setattr(module, '_CHILD_CODE', code)
        argv = cli_args(collection)
        argv[argv.index('--url') + 1] = 'http://127.0.0.1:' + str(server.server_port)
        started = time.monotonic()
        assert module.main(argv) == 2
        assert time.monotonic() - started < 1.8
        assert reached.is_set()  # An unrelated preflight error cannot satisfy this test.
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)
    assert not collection[2]['destination'].exists()


def test_cli_cutoff_preserves_saved_evidence_for_valid_restart(collection, monkeypatch):
    import time
    import skybuild.manual_result as module
    collect(collection)
    path = collection[2]['destination']
    original = path.read_bytes()
    path.unlink()
    code = ("import sys; sys.path.insert(0," + repr(str(module.Path(module.__file__).resolve().parents[1])) +
            "); import time, signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); from pathlib import Path; from skybuild.manual_cord import _private_write; "
            "_private_write(Path(" + repr(str(path)) + "), " + repr(original) + "); time.sleep(3)")
    monkeypatch.setattr(module, '_CHILD_CODE', code)
    started = time.monotonic()
    assert module.main(cli_args(collection)) == 2
    assert time.monotonic() - started < 1.8
    assert path.read_bytes() == original
    actions = collection[5]
    previous = actions[-1]
    assert collect(collection)['receipted'] is True
    assert actions[-1] == previous and path.read_bytes() == original


def test_cli_cutoff_kills_owned_git_ssh_group_and_preserves_unrelated_child(collection, tmp_path, monkeypatch):
    import os
    import sys
    import time
    import skybuild.manual_result as module
    marker = tmp_path / 'ssh.pid'
    helper = tmp_path / 'ssh-helper'
    helper.write_text('#!' + sys.executable + '\n' +
        'import os, signal, time\nfrom pathlib import Path\n' +
        'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n' +
        'Path(' + repr(str(marker)) + ').write_text(str(os.getpid()))\ntime.sleep(10)\n')
    helper.chmod(0o700)
    monkeypatch.setattr(module, '_CHILD_CODE',
        "import subprocess, os; env=dict(os.environ, GIT_SSH_COMMAND=" + repr(str(helper)) +
        ", GIT_SSH_VARIANT='ssh'); subprocess.run(['git','ls-remote',"
        "'ssh://synthetic.invalid/no-repo'], capture_output=True, timeout=10, env=env)")
    foreign = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)'])
    owned = None
    try:
        started = time.monotonic()
        assert module.main(cli_args(collection)) == 2
        assert time.monotonic() - started < 1.8
        owned = int(marker.read_text())  # The real Git SSH subprocess was reached.
        # A killed orphan may briefly remain a zombie until its parent reaps it.
        stat = module.Path('/proc') / str(owned) / 'stat'
        cleanup_deadline = time.monotonic() + 0.2
        while stat.exists() and not stat.read_text().split(') ')[1].startswith('Z '):
            assert time.monotonic() < cleanup_deadline
            time.sleep(0.01)
        assert foreign.poll() is None
    finally:
        foreign.terminate()
        foreign.wait(timeout=1)
        if owned is not None:
            try:
                os.kill(owned, 9)  # Test-owned cleanup if the regression fails.
            except ProcessLookupError:
                pass
