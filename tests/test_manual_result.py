"""Bounded dispatcher collection with scratch Git and synthetic REST only."""
from datetime import datetime, timezone
import json
import copy
import hashlib
import subprocess

import pytest

from skybuild.manual_result import ResultError, receive_result
from skybuild.manual_cord import ManualCordError
from test_manual_assignment import pinned  # noqa: F401


@pytest.fixture
def collection(pinned, tmp_path):
    repo, assignment = pinned
    assignment.update(schema='manual-work-v2', task_status='ready', task_revision=3)
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
        task = {'project_id': 'skybuild', 'task_id': assignment['task_id'],
                'revision': 3, 'status': 'ready', 'metadata': {}}

        def get_task(self, project, task_id):
            assert project == 'skybuild' and task_id == assignment['task_id']
            return copy.deepcopy(self.task)

        def whoami(self):
            return {'principal_id': assignment['dispatcher'], 'is_admin': False,
                    'grants': {'skybuild': ['tasks:read', 'cord:send', 'cord:read', 'cord:handle']}}

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


@pytest.mark.parametrize('change', ['admin', 'principal', 'project', 'missing', 'extra', 'duplicate', 'malformed'])
def test_collector_requires_exact_dispatcher_profile(collection, change):
    client, _, kwargs, _, _, actions, *_ = collection
    identity = client.whoami()
    scopes = identity['grants']['skybuild']
    if change == 'admin':
        identity['is_admin'] = True
    elif change == 'principal':
        identity['principal_id'] = 'another-dispatcher'
    elif change == 'project':
        identity['grants']['foreign'] = list(scopes)
    elif change == 'missing':
        scopes.remove('tasks:read')
    elif change == 'extra':
        scopes.append('tasks:write')
    elif change == 'duplicate':
        scopes[-1] = scopes[0]
    else:
        scopes[-1] = ['cord:handle']
    client.whoami = lambda: identity
    with pytest.raises(ResultError, match='scoped dispatcher'):
        collect(collection)
    assert not kwargs['destination'].exists()
    assert actions == []


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
    assert result['review_required'] is True
    assert 'checks' not in result and 'risks' not in result


@pytest.mark.parametrize(('phase', 'routing', 'review_required'), [
    ('in-progress', 'record-only', False),
    ('blocked', 'owner-attention', False),
    ('ready-for-review', 'queue-independent-review', True),
])
def test_result_phase_routes_without_starting_model_work(collection, phase, routing, review_required):
    client, _, _, result, message, actions, *_ = collection
    result['phase'] = phase
    message['body'] = json.dumps(result)

    collected = collect(collection)

    assert collected['phase'] == phase
    assert collected['routing'] == routing
    assert collected['review_required'] is review_required
    assert len(actions) == 1 and actions[0][2] == 'receipt'


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



def cli_args(collection, *, duration=1):
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
              'destination': kwargs['destination'], 'duration': duration}
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
    r->retrans = 30; r->retry = 1;
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
        # Include a slow cold start in the wall budget, but still reach real DNS.
        duration = 5
        prefix = ("import time; time.sleep(1.1); import ctypes; lib=ctypes.CDLL(" + repr(str(library)) +
                  "); assert lib.configure_resolver(" + str(resolver.getsockname()[1]) + ") == 0; ")
        monkeypatch.setattr(module, '_CHILD_CODE', prefix + module._CHILD_CODE)
        started = time.monotonic()
        assert module.main(cli_args(collection, duration=duration)) == 2
        assert time.monotonic() - started < duration + 0.8
        assert resolver.recvfrom(4096)[0]  # Actual libc resolver reached the dropped UDP reply.
    assert not collection[2]['destination'].exists()
    stderr = capsys.readouterr().err
    assert 'synthetic-token' not in stderr
    assert json.loads(stderr)['child_termination'] == 'confirmed'


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
        duration = 5
        code = 'import time; time.sleep(1.1); ' + module._CHILD_CODE.replace('from skybuild.manual_result import _main;',
            'import skybuild.manual_result as m; '
            'm._private_endpoint=lambda url,resolver:url; '
            'm.receive_result=lambda client,*args,**kwargs:client.whoami(); '
            'from skybuild.manual_result import _main;')
        monkeypatch.setattr(module, '_CHILD_CODE', code)
        argv = cli_args(collection, duration=duration)
        argv[argv.index('--url') + 1] = 'http://127.0.0.1:' + str(server.server_port)
        # Server thread belongs to the test process, not the standalone CLI.
        # Measure main's budget after the separate parent imports its modules.
        script = ('import sys; sys.path.insert(0,' +
                  repr(str(module.Path(module.__file__).resolve().parents[1])) +
                  '); import skybuild.manual_result as m; m._CHILD_CODE=' + repr(code) +
                  '; import time, json; started=time.monotonic(); result=m.main(' + repr(argv) +
                  '); print(json.dumps({"elapsed":time.monotonic()-started})); raise SystemExit(result)')
        outcome = subprocess.run([module.sys.executable, '-c', script],
                                 capture_output=True, timeout=duration + 10)
        assert outcome.returncode == 2
        assert json.loads(outcome.stdout)['elapsed'] < duration + 0.8
        assert json.loads(outcome.stderr)['child_termination'] == 'confirmed'
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
    duration = 5
    code = ("import time; time.sleep(1.1); import sys; sys.path.insert(0," + repr(str(module.Path(module.__file__).resolve().parents[1])) +
            "); import time, signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); from pathlib import Path; from skybuild.manual_cord import _private_write; "
            "_private_write(Path(" + repr(str(path)) + "), " + repr(original) + "); time.sleep(15)")
    monkeypatch.setattr(module, '_CHILD_CODE', code)
    started = time.monotonic()
    assert module.main(cli_args(collection, duration=duration)) == 2
    assert time.monotonic() - started < duration + 0.8
    assert path.read_bytes() == original
    actions = collection[5]
    previous = actions[-1]
    assert collect(collection)['receipted'] is True
    assert actions[-1] == previous and path.read_bytes() == original


@pytest.mark.parametrize('exit_path', ['outer_timeout', 'inner_git_timeout', 'success', 'interruption'])
def test_cli_all_exit_paths_clean_owned_group_and_preserve_unrelated_child(collection, tmp_path, monkeypatch, exit_path):
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
    timeout = 0.2 if exit_path == 'inner_git_timeout' else 10
    code = ("import subprocess, os; env=dict(os.environ, GIT_SSH_COMMAND=" + repr(str(helper)) +
        ", GIT_SSH_VARIANT='ssh'); subprocess.run(['git','ls-remote',"
        "'ssh://synthetic.invalid/no-repo'], capture_output=True, timeout=" + str(timeout) + ", env=env)")
    if exit_path == 'success':
        code = ("import subprocess, time; from pathlib import Path; subprocess.Popen([" + repr(str(helper)) +
                "]); marker=Path(" + repr(str(marker)) + "); "
                "exec('while not marker.exists(): time.sleep(0.01)'); print('{\"receipted\": true}')")
    monkeypatch.setattr(module, '_CHILD_CODE', code)
    if exit_path == 'interruption':
        waitid = os.waitid
        interrupted = False
        def interrupt_after_ssh(*args):
            nonlocal interrupted
            if marker.exists() and not interrupted:
                interrupted = True
                raise KeyboardInterrupt
            return waitid(*args)
        monkeypatch.setattr(os, 'waitid', interrupt_after_ssh)
    foreign = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)'])
    owned = None
    try:
        started = time.monotonic()
        if exit_path == 'interruption':
            with pytest.raises(KeyboardInterrupt):
                module.main(cli_args(collection))
        else:
            assert module.main(cli_args(collection)) == (0 if exit_path == 'success' else 2)
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


@pytest.mark.parametrize('handler', ['ignore', 'custom'])
def test_cli_refuses_incompatible_child_reaping_before_launch(collection, monkeypatch, handler):
    import signal
    import skybuild.manual_result as module
    previous = signal.getsignal(signal.SIGCHLD)
    try:
        signal.signal(signal.SIGCHLD, signal.SIG_IGN if handler == 'ignore' else lambda *args: None)
        monkeypatch.setattr(module.subprocess, 'Popen', lambda *args, **kwargs: pytest.fail('Child launched'))
        monkeypatch.setattr(module.os, 'killpg', lambda *args: pytest.fail('Group signaled'))
        assert module.main(cli_args(collection)) == 2
    finally:
        signal.signal(signal.SIGCHLD, previous)


def test_cli_unexpected_reaping_never_signals_unreserved_group(collection, monkeypatch):
    import os
    import skybuild.manual_result as module
    monkeypatch.setattr(module, '_CHILD_CODE', 'pass')
    def externally_reaped(kind, pid, flags):
        os.waitpid(pid, 0)
        raise ChildProcessError('synthetic external reaping')
    monkeypatch.setattr(os, 'waitid', externally_reaped)
    monkeypatch.setattr(os, 'killpg', lambda *args: pytest.fail('Unreserved group signaled'))
    assert module.main(cli_args(collection)) == 2


@pytest.mark.parametrize('change', ['revision', 'status', 'project', 'task', 'metadata', 'unbound', 'unavailable'])
def test_stale_legacy_result_retains_evidence_but_blocks_review(collection, change):
    client, _, kwargs, _, _, actions, *_ = collection
    if change == 'revision':
        client.task['revision'] = 4
    elif change == 'status':
        client.task['status'] = 'blocked'
    elif change == 'project':
        client.task['project_id'] = 'foreign'
    elif change == 'task':
        client.task['task_id'] = 'OTHER'
    elif change == 'metadata':
        client.task['metadata'] = {'_skybuild_workflow': {'petri': {'schema_version': 2}}}
    elif change == 'unbound':
        kwargs['assignment'].update(schema='manual-work-v1')
        kwargs['assignment'].pop('task_status')
        kwargs['assignment'].pop('task_revision')
    else:
        def unavailable(*args):
            raise TimeoutError('private transport details')
        client.get_task = unavailable
    output = collect(collection)
    assert output['routing'] == 'owner-attention' and output['review_required'] is False
    assert 'private transport' not in json.dumps(output)
    assert kwargs['destination'].exists() and len(actions) == 1


@pytest.fixture
def submitted_collection(collection, tmp_path):
    from skybuild.workflow import TaskToken, Place
    client, _, kwargs, result, *_ = collection
    assignment = kwargs['assignment']
    claimed = TaskToken(project_id='skybuild', task_id=assignment['task_id'], place=Place.WORKING,
                        revision=4, attempt_id='attempt-001', claim_fence=1, input_generation=7,
                        definition_revision=2, policy_version='policy-1',
                        source_head=assignment['base_sha'], target_base=assignment['base_sha']).to_dict()
    receipt = {name: claimed[name] for name in ('attempt_id', 'claim_fence', 'input_generation',
                                               'definition_revision', 'policy_version')}
    receipt.update(source_head=result['head_sha'], source_branch='refs/heads/' + assignment['branch'],
                   target_base=assignment['base_sha'])
    submitted = {**claimed, **receipt, 'input_generation': 8, 'revision': 5, 'place': 'validating'}
    task = {'project_id': 'skybuild', 'task_id': assignment['task_id'], 'revision': 5,
            'status': 'in-progress', 'metadata': {'_skybuild_workflow': {
                'petri': {'schema_version': 1, 'token': submitted}}}}
    state = {'schema': 'manual-petri-claim-v1', 'project_id': 'skybuild', 'worker': assignment['worker'],
             'assignment_id': assignment['assignment_id'], 'task_id': assignment['task_id'],
             'expected_revision': 3,
             'assignment_sha256': hashlib.sha256(json.dumps(assignment, sort_keys=True).encode()).hexdigest(),
             'token': claimed, 'claim': {'holder': assignment['worker'], 'held': True, 'fence': 1, 'task_revision': 4}}
    intent = {'body': receipt, 'expected_revision': 4, 'idempotency_key': 'submit-key'}
    binding = tmp_path / 'assignment.workflow.json'
    intent_path = binding.with_name(binding.name + '.submit')
    for path, value in ((binding, state), (intent_path, intent)):
        path.write_text(json.dumps(value))
        path.chmod(0o600)
    kwargs['workflow_binding'] = binding
    client.task = task
    client.workflow = {'token': submitted}
    client.history = [{'project_id': 'skybuild', 'task_id': assignment['task_id'],
                       'actor': assignment['worker'], 'operation': 'workflow.submit', 'revision': 5,
                       'event_facts': {'author_output_receipt': receipt}, 'after_state': copy.deepcopy(task)}]
    reads = []

    def history(project, task_id, *, limit, offset):
        assert (project, task_id, limit, offset) == ('skybuild', assignment['task_id'], 1, intent['expected_revision'])
        reads.append('history')
        return copy.deepcopy(client.history)

    def workflow(project, task_id):
        reads.append('workflow')
        return copy.deepcopy(client.workflow)

    client.task_history = history
    client.task_workflow = workflow
    return collection, state, intent, reads


def test_petri_submission_advances_revision_and_generation_without_stale_rejection(submitted_collection):
    collection, _, _, reads = submitted_collection
    output = collect(collection)
    assert output['routing'] == 'queue-independent-review' and output['review_required'] is True
    assert reads == ['history', 'workflow']


@pytest.mark.parametrize('field,value', [
    ('attempt_id', 'other-attempt'), ('claim_fence', 2), ('input_generation', 9),
    ('definition_revision', 3), ('policy_version', 'policy-2'), ('source_head', 'f' * 40),
    ('source_branch', 'refs/heads/task/other'), ('target_base', 'e' * 40),
    ('place', 'hold'), ('pending_action', 'reconcile'), ('superseded', True),
])
def test_petri_stale_inputs_and_pending_effects_never_queue_review(submitted_collection, field, value):
    collection, *_ = submitted_collection
    client, _, kwargs, _, _, actions, *_ = collection
    client.task['metadata']['_skybuild_workflow']['petri']['token'][field] = value
    output = collect(collection)
    assert output['routing'] == 'owner-attention' and output['review_required'] is False
    assert kwargs['destination'].exists() and len(actions) == 1


@pytest.mark.parametrize('change', ['missing', 'public', 'symlink', 'fingerprint', 'holder', 'fence',
                                    'intent', 'journal', 'actor', 'unsubmitted', 'race', 'malformed'])
def test_petri_requires_private_durable_binding_and_authoritative_submission(submitted_collection, change):
    collection, state, intent, _ = submitted_collection
    client, _, kwargs, *_ = collection
    binding = kwargs['workflow_binding']
    if change == 'missing':
        kwargs['workflow_binding'] = None
    elif change == 'public':
        binding.chmod(0o644)
    elif change == 'symlink':
        saved = binding.with_suffix('.saved')
        binding.rename(saved)
        binding.symlink_to(saved)
    elif change in {'fingerprint', 'holder', 'fence'}:
        if change == 'fingerprint':
            state['assignment_sha256'] = '0' * 64
        elif change == 'holder':
            state['claim']['holder'] = 'foreign'
        else:
            state['claim']['fence'] = 2
        binding.write_text(json.dumps(state))
    elif change == 'intent':
        intent['body']['source_head'] = 'f' * 40
        binding.with_name(binding.name + '.submit').write_text(json.dumps(intent))
    elif change == 'journal':
        client.history[0]['event_facts']['author_output_receipt']['input_generation'] = 99
    elif change == 'actor':
        client.history[0]['actor'] = 'foreign'
    elif change == 'unsubmitted':
        client.history = []
    elif change == 'malformed':
        client.workflow['token']['revision'] = True
    else:
        original = client.get_task
        count = [0]
        def race(*args):
            count[0] += 1
            task = original(*args)
            if count[0] == 2:
                task['revision'] += 1
            return task
        client.get_task = race
    output = collect(collection)
    assert output['routing'] == 'owner-attention' and output['review_required'] is False
    assert kwargs['destination'].exists()


def test_freshness_deadline_expiry_keeps_evidence_without_receipt(collection):
    client, _, kwargs, _, _, actions, _, now, *_ = collection
    original = client.get_task
    def late(*args):
        now[0] = 2001
        return original(*args)
    client.get_task = late
    with pytest.raises(ResultError, match='deadline'):
        collect(collection)
    assert kwargs['destination'].exists() and actions == []


def test_later_coherent_attempt_cannot_attach_to_stale_assignment(submitted_collection):
    collection, state, intent, reads = submitted_collection
    client, _, kwargs, *_ = collection
    state['token']['revision'] = state['claim']['task_revision'] = 8
    intent['expected_revision'] = 8
    binding = kwargs['workflow_binding']
    binding.write_text(json.dumps(state))
    binding.with_name(binding.name + '.submit').write_text(json.dumps(intent))
    client.history[0]['revision'] = 9
    for task in (client.task, client.history[0]['after_state']):
        task['revision'] = 9
        task['metadata']['_skybuild_workflow']['petri']['token']['revision'] = 9
    client.workflow['token']['revision'] = 9
    output = collect(collection)
    assert output['routing'] == 'owner-attention' and output['review_required'] is False
    assert kwargs['destination'].exists() and reads == []


def test_cli_accepts_explicit_workflow_binding(collection):
    from skybuild.manual_result import _parse_args
    args = _parse_args(cli_args(collection) + ['--workflow-binding', '/tmp/private.workflow.json'])
    assert str(args.workflow_binding) == '/tmp/private.workflow.json'
