"""Collect one pinned manual worker result; receipt is not review or acceptance."""
import argparse
from datetime import datetime
import hashlib
import json
import os
import stat
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time

from .client import Client, ClientError
from .contracts import DomainError, valid_identifier
from .fleet_preflight import _resolved_addresses, _token_from_file
from .manual_assignment import AssignmentError, _path, verify_assignment
from .manual_cord import ManualCordError, _private_write
from .manual_dispatch import _private_endpoint
from .workflow import TaskToken


class ResultError(ValueError):
    pass


def _task_token(task, project, task_id):
    if (not isinstance(task, dict) or task.get('project_id') != project
            or task.get('task_id') != task_id or type(task.get('revision')) is not int
            or task['revision'] < 1 or not isinstance(task.get('metadata'), dict)):
        raise ResultError('Current task identity is unavailable')
    workflow = task['metadata'].get('_skybuild_workflow', {})
    if not isinstance(workflow, dict):
        raise ResultError('Current workflow metadata is invalid')
    if 'petri' not in workflow:
        return None
    petri = workflow['petri']
    if not isinstance(petri, dict) or petri.get('schema_version') != 1:
        raise ResultError('Current workflow version is unsupported')
    token = TaskToken.from_dict(petri.get('token')).to_dict()
    if (token['project_id'] != project or token['task_id'] != task_id
            or token['revision'] != task['revision']):
        raise ResultError('Current task and token differ')
    return token


def _review_freshness(client, call, project, assignment, result, workflow_binding):
    """Bind review to current REST authority, never to Git evidence alone."""
    task_id = assignment['task_id']
    if assignment.get('schema') != 'manual-work-v2':
        raise ResultError('Review requires an API-bound assignment')
    task = call(client.get_task, project, task_id)
    current = _task_token(task, project, task_id)
    if current is None:
        if (task['revision'] != assignment['task_revision']
                or task.get('status') != assignment['task_status']
                or task.get('status') not in {'ready', 'in-progress'}):
            raise ResultError('Legacy assignment is stale')
        return
    if workflow_binding is None:
        raise ResultError('Petri review requires the durable worker binding')
    state = _read_assignment(workflow_binding, private=True)
    intent = _read_assignment(workflow_binding.with_name(workflow_binding.name + '.submit'), private=True)
    pins = {'schema': 'manual-petri-claim-v1', 'project_id': project,
            'worker': assignment['worker'], 'assignment_id': assignment['assignment_id'],
            'task_id': task_id, 'expected_revision': assignment['task_revision'],
            'assignment_sha256': hashlib.sha256(json.dumps(assignment, sort_keys=True).encode()).hexdigest()}
    if not isinstance(state, dict) or any(state.get(key) != value for key, value in pins.items()):
        raise ResultError('Petri assignment binding differs')
    claimed = TaskToken.from_dict(state.get('token')).to_dict()
    claim = state.get('claim')
    if (not isinstance(claim, dict) or claim.get('held') is not True
            or claim.get('holder') != assignment['worker'] or type(claim.get('fence')) is not int
            or claim['fence'] != claimed['claim_fence'] or not claimed['attempt_id']
            or claimed['project_id'] != project or claimed['task_id'] != task_id
            or claimed['place'] != 'working' or claimed['pending_action'] is not None
            or claimed['superseded'] or claimed['revision'] != claim.get('task_revision')
            or claimed['revision'] != assignment['task_revision'] + 1):
        raise ResultError('Petri claim binding is invalid')
    receipt = {name: claimed[name] for name in ('attempt_id', 'claim_fence', 'input_generation',
                                               'definition_revision', 'policy_version')}
    receipt.update(source_head=result['head_sha'], source_branch='refs/heads/' + assignment['branch'],
                   target_base=assignment['base_sha'])
    if (not isinstance(intent, dict) or intent.get('body') != receipt
            or type(intent.get('expected_revision')) is not int
            or intent['expected_revision'] < claimed['revision']):
        raise ResultError('Petri submission intent differs')
    # Submission can legitimately advance revision and input generation when
    # publishing the author head. Read its immutable journal event rather than
    # guessing the resulting generation from the pre-claim assignment.
    history = call(client.task_history, project, task_id, limit=1, offset=intent['expected_revision'])
    event = history[0] if isinstance(history, list) and len(history) == 1 else None
    if (not isinstance(event, dict) or event.get('project_id') != project
            or event.get('task_id') != task_id or event.get('actor') != assignment['worker']
            or event.get('operation') != 'workflow.submit'
            or event.get('revision') != intent['expected_revision'] + 1
            or not isinstance(event.get('event_facts'), dict)
            or event['event_facts'].get('author_output_receipt') != receipt):
        raise ResultError('Petri submission is unconfirmed')
    submitted = _task_token(event.get('after_state'), project, task_id)
    if (submitted is None or submitted['place'] != 'validating'
            or submitted['revision'] != event['revision']
            or any(submitted[name] != receipt[name] for name in
                   ('attempt_id', 'claim_fence', 'definition_revision', 'policy_version',
                    'source_head', 'source_branch', 'target_base'))
            or submitted['input_generation'] < claimed['input_generation']):
        raise ResultError('Petri submitted attempt differs')
    view = call(client.task_workflow, project, task_id)
    current = TaskToken.from_dict(view.get('token') if isinstance(view, dict) else None).to_dict()
    # A final task read detects changes during the journal/workflow reads.
    latest = call(client.get_task, project, task_id)
    if (_task_token(latest, project, task_id) != current or current['place'] != 'validating'
            or current['pending_action'] is not None or current['superseded']
            or current['revision'] < submitted['revision']
            or any(current[name] != submitted[name] for name in
                   ('project_id', 'task_id', 'attempt_id', 'claim_fence', 'input_generation',
                    'definition_revision', 'policy_version', 'source_head', 'source_branch', 'target_base'))):
        raise ResultError('Petri result is stale or pending reconciliation')


def receive_result(client, project, checkout, *, assignment, assignment_id, task_id,
                   worker, dispatcher, base_sha, message_id, destination,
                   approval_until, duration=120, max_pages=3, workflow_binding=None,
                   clock=time.time, monotonic=time.monotonic, git_runner=None):
    """Persist verified evidence before receipt, without handling the message.

    Injected transports/runners must honor finite timeouts. Every external call
    and the receipt require remaining wall and monotonic deadline budget.
    """
    if (type(duration) is not int or not 1 <= duration <= 120
            or type(max_pages) is not int or not 1 <= max_pages <= 10):
        raise ResultError("Collection bounds are invalid")
    if not all(valid_identifier(value) for value in (project, worker, dispatcher, message_id)):
        raise ResultError("Collection identity is invalid")
    try:
        expiry = datetime.fromisoformat(approval_until.replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            raise ValueError
        expires = expiry.timestamp()
    except (AttributeError, ValueError, OverflowError):
        raise ResultError("An offset-aware approval cutoff is required") from None
    budget = min(duration, expires - clock())
    end = monotonic() + budget

    def remaining():
        seconds = min(expires - clock(), end - monotonic())
        if seconds <= 0:
            raise ResultError("Collection deadline expired; preserve local evidence")
        return seconds

    def git(repo, *args):
        timeout = min(10, remaining())
        if git_runner is not None:
            output = git_runner(repo, *args, timeout=timeout)
        else:
            result = subprocess.run(["git", *args], cwd=repo, capture_output=True,
                                    check=False, timeout=timeout)
            if result.returncode:
                raise ResultError("Pinned result Git evidence is unavailable")
            output = result.stdout
        remaining()
        return output

    def call(method, *args, **kwargs):
        remaining()
        # The production Client uses no retries. Cap each request to the budget;
        # callbacks in offline tests have the same finite-timeout obligation.
        if isinstance(client, Client):
            import httpx
            client.http.timeout = httpx.Timeout(min(5, remaining()))
        value = method(*args, **kwargs)
        remaining()
        return value

    remaining()
    if isinstance(client, Client) and client.retries != 0:
        raise ResultError("Collector transport must disable automatic retries")
    snapshot = verify_assignment(assignment, checkout, worker=worker, git_runner=git)
    if any(assignment.get(key) != value for key, value in {
            'assignment_id': assignment_id, 'task_id': task_id, 'worker': worker,
            'dispatcher': dispatcher, 'base_sha': base_sha}.items()):
        raise ResultError("Assignment differs from explicit collection pins")
    identity = call(client.whoami)
    scopes = identity.get('grants', {}).get(project) if isinstance(identity, dict) and isinstance(identity.get('grants'), dict) else None
    if (not isinstance(identity, dict) or identity.get('principal_id') != dispatcher
            or identity.get('is_admin') is not False or set(identity.get('grants', {})) != {project}
            or not isinstance(scopes, list) or len(scopes) != 4
            or not all(isinstance(scope, str) for scope in scopes)
            or set(scopes) != {'tasks:read', 'cord:read', 'cord:send', 'cord:handle'}):
        raise ResultError("Collector credential differs from scoped dispatcher")
    found = None
    for page in range(max_pages):
        messages = call(client.inbox, project, limit=100, offset=page * 100)
        if not isinstance(messages, list) or len(messages) > 100:
            raise ResultError("Result inbox page is invalid")
        matches = [item for item in messages if isinstance(item, dict) and item.get('message_id') == message_id]
        if len(matches) > 1:
            raise ResultError("Result message identity is duplicated")
        if matches:
            found = matches[0]
            break
        if len(messages) < 100:
            break
    if found is None:
        raise ResultError("Pinned result is absent from bounded inbox pages")
    if (found.get('sender') != worker or found.get('recipient') != dispatcher
            or found.get('category') != 'manual-work'):
        raise ResultError("Result sender, recipient or category differs")
    body = found.get('body')
    if not isinstance(body, str) or len(body.encode('utf-8')) > 32768:
        raise ResultError("Result body exceeds the bounded contract")
    try:
        result = json.loads(body)
    except ValueError:
        raise ResultError("Result body is invalid JSON") from None
    fields = {'schema', 'assignment_id', 'phase', 'branch', 'head_sha', 'checks',
              'changed_paths', 'risks', 'next_action'}
    if (not isinstance(result, dict) or set(result) != fields or result.get('schema') != 'manual-work-v1'
            or result.get('assignment_id') != assignment_id or result.get('branch') != snapshot['branch']
            or result.get('phase') not in {'in-progress', 'blocked', 'ready-for-review'}
            or not isinstance(result.get('head_sha'), str) or not re.fullmatch('[0-9a-f]{40}', result['head_sha'])):
        raise ResultError("Result differs from pinned assignment or report contract")
    for key in ('checks', 'changed_paths', 'risks'):
        values = result[key]
        if (not isinstance(values, list) or len(values) > 100
                or any(not isinstance(value, str) or not value or len(value) > 500 or '\x00' in value for value in values)):
            raise ResultError("Result evidence is invalid or unbounded")
    if not isinstance(result['next_action'], str) or not result['next_action'].strip() or len(result['next_action']) > 500 or '\x00' in result['next_action']:
        raise ResultError("Result needs a bounded next action")
    paths = [_path(value) for value in result['changed_paths']]
    if len(paths) != len(set(paths)) or any(not any(path == owned or path.startswith(owned + '/')
                                                 for owned in snapshot['owned_paths']) for path in paths):
        raise ResultError("Result changed paths escape assignment ownership")
    head = result['head_sha']
    advertised = git(checkout, 'ls-remote', '--exit-code', 'origin', 'refs/heads/' + snapshot['branch']).decode().strip()
    if advertised != head + '\trefs/heads/' + snapshot['branch']:
        raise ResultError("Reported head is not the exact pushed task branch")
    git(checkout, 'cat-file', '-e', head + '^{commit}')
    git(checkout, 'merge-base', '--is-ancestor', base_sha, head)
    changed = git(checkout, 'diff', '--no-renames', '--name-only', '-z', base_sha, head)
    actual = [value.decode('utf-8') for value in changed.split(b'\x00') if value]
    if sorted(actual) != sorted(paths):
        raise ResultError("Result changed paths differ from pinned Git evidence")
    record = {'schema': 'manual-collected-result-v1', 'project_id': project,
              'message_id': message_id, 'assignment': assignment, 'result': result,
              'sender': worker, 'recipient': dispatcher}
    remaining()
    _private_write(destination, (json.dumps(record, sort_keys=True, ensure_ascii=False) + '\n').encode('utf-8'))
    remaining()
    freshness = None
    if result['phase'] == 'ready-for-review':
        try:
            _review_freshness(client, call, project, assignment, result, workflow_binding)
        except (ResultError, DomainError, ClientError, OSError, ValueError, TypeError, AttributeError):
            # Keep independently verified Git/result evidence. Transport errors
            # and malformed or absent bindings cannot authorize review.
            freshness = 'Current task or durable submission binding is unavailable or stale'
        remaining()
    key = 'manual-result-receipt-' + hashlib.sha256((project + '\n' + message_id).encode()).hexdigest()
    reply = call(client.message_action, project, message_id, 'receipt', idempotency_key=key)
    if not isinstance(reply, dict) or reply.get('message_id') != message_id or not reply.get('delivered_at'):
        raise ResultError("Result receipt is unconfirmed; preserve local evidence")
    phase = result['phase']
    routing = {
        'in-progress': 'record-only',
        'blocked': 'owner-attention',
        'ready-for-review': 'queue-independent-review',
    }[phase]
    if freshness is not None:
        routing = 'owner-attention'
    return {'saved': str(destination), 'message_id': message_id, 'assignment_id': assignment_id,
            'task_id': task_id, 'head_sha': head, 'phase': phase, 'receipted': True,
            'routing': routing, 'review_required': routing == 'queue-independent-review',
            **({'freshness_reason': freshness} if freshness is not None else {})}


def _read_assignment(path, *, private=False):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 65536:
            raise ResultError("Assignment input must be a bounded ordinary file")
        if private and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise ResultError('Workflow binding must be an owned private file')
        data = os.read(descriptor, 65537)
        if len(data) > 65536:
            raise ResultError("Assignment input exceeds its bound")
        return json.loads(data)
    finally:
        os.close(descriptor)


def _parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('url', 'project', 'worker', 'dispatcher', 'assignment-id', 'task-id',
                 'base-sha', 'message-id', 'approval-until'):
        parser.add_argument('--' + name, required=True)
    for name in ('token-file', 'checkout', 'assignment', 'destination'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--ca-file', type=Path)
    parser.add_argument('--workflow-binding', type=Path)
    parser.add_argument('--duration', type=int, default=120)
    parser.add_argument('--max-pages', type=int, default=3)
    return parser.parse_args(argv)


def _main(argv=None):
    args = _parse_args(argv)
    try:
        started = time.monotonic()
        if not 1 <= args.duration <= 120 or not 1 <= args.max_pages <= 10:
            raise ResultError("Collection bounds are invalid")
        # Validate cutoff before DNS, credentials or REST. receive_result repeats
        # it before every external call and before receipt.
        cutoff = datetime.fromisoformat(args.approval_until.replace('Z', '+00:00'))
        if cutoff.tzinfo is None or cutoff.timestamp() <= time.time():
            raise ResultError('Collection deadline expired')
        return _run_cli(args, cutoff, started)
    except (ResultError, AssignmentError, ManualCordError, ClientError, OSError, ValueError,
            TypeError, UnicodeError, subprocess.SubprocessError):
        print(json.dumps({'ok': False, 'reason': 'Result collection failed; preserve local evidence'}), file=sys.stderr)
        return 2


def _run_cli(args, cutoff, started):
    endpoint = _private_endpoint(args.url, _resolved_addresses)
    assignment = _read_assignment(args.assignment)
    remaining = args.duration - (time.monotonic() - started)
    duration = max(1, int(remaining))
    if remaining <= 0 or cutoff.timestamp() <= time.time():
        raise ResultError('Collection deadline expired')
    with Client(endpoint, _token_from_file(args.token_file), retries=0, timeout=5,
                trust_env=False, ca_file=args.ca_file) as client:
        output = receive_result(client, args.project, args.checkout, assignment=assignment,
            assignment_id=args.assignment_id, task_id=args.task_id, worker=args.worker,
            dispatcher=args.dispatcher, base_sha=args.base_sha, message_id=args.message_id,
            destination=args.destination, approval_until=args.approval_until,
            duration=duration, max_pages=args.max_pages, workflow_binding=args.workflow_binding)
    print(json.dumps(output, sort_keys=True))
    return 0


# This is a collector subprocess, never a model/task launcher. Keep its source
# import pinned to the same package as the supervising entry point.
_CHILD_CODE = ("import sys; sys.path.insert(0, " + repr(str(Path(__file__).resolve().parents[1])) +
               "); from skybuild.manual_result import _main; raise SystemExit(_main(sys.argv[1:]))")
_TERMINATION_GRACE = 0.5


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    args = _parse_args(argv)
    failure = {'ok': False, 'reason': 'Result collection failed; preserve local evidence'}
    try:
        if (threading.current_thread() is not threading.main_thread() or
                threading.active_count() != 1 or signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL):
            raise ResultError('Collection requires standalone default child reaping')
        if not 1 <= args.duration <= 120 or not 1 <= args.max_pages <= 10:
            raise ResultError("Collection bounds are invalid")
        cutoff = datetime.fromisoformat(args.approval_until.replace('Z', '+00:00'))
        if cutoff.tzinfo is None:
            raise ResultError("An offset-aware approval cutoff is required")
        budget = min(args.duration, cutoff.timestamp() - time.time())
        if budget <= 0:
            raise ResultError("Collection deadline expired")
        deadline = time.monotonic() + budget
        with tempfile.TemporaryFile() as captured:
            child = subprocess.Popen([sys.executable, '-c', _CHILD_CODE, *argv],
                                     stdin=subprocess.DEVNULL, stdout=captured,
                                     stderr=subprocess.DEVNULL, start_new_session=True)
            expired = False
            leader_reserved = True
            try:
                # Observe exit without reaping: the owned session/group identifier
                # stays reserved until all final group signals have been sent.
                while os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
                    remaining = min(deadline - time.monotonic(), cutoff.timestamp() - time.time())
                    if remaining <= 0:
                        expired = True
                        break
                    time.sleep(min(0.01, remaining))
            except ChildProcessError:
                # An unexpected reaper invalidates group ownership. Never signal
                # a numeric identifier after its leader has been reaped.
                leader_reserved = False
                raise ResultError('Collector child ownership is unknown') from None
            finally:
                if leader_reserved:
                    # Run on success, failure and interruption, including an inner Git
                    # timeout that leaves an SSH descendant after its caller exits.
                    try:
                        os.killpg(child.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        time.sleep(_TERMINATION_GRACE / 2)
                    finally:
                        try:
                            os.killpg(child.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        try:
                            child.wait(timeout=_TERMINATION_GRACE / 2)
                        except subprocess.TimeoutExpired:
                            pass  # Kernel completion is unknown; never wait forever.
            if expired:
                failure.update(receipt_state='unknown',
                               child_termination='confirmed' if child.poll() is not None else 'unknown',
                               termination_grace_seconds=_TERMINATION_GRACE)
                raise ResultError("Collection deadline expired")
            captured.seek(0)
            output = captured.read(16385)
        if time.monotonic() >= deadline or time.time() >= cutoff.timestamp():
            raise ResultError("Collection deadline expired")
        if child.returncode != 0 or len(output) > 16384:
            raise ResultError("Collection did not produce confirmed evidence")
        result = json.loads(output)
        if not isinstance(result, dict) or result.get('receipted') is not True:
            raise ResultError("Collection result is invalid")
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ResultError, OSError, ValueError, TypeError, UnicodeError, subprocess.SubprocessError):
        print(json.dumps(failure), file=sys.stderr)
        return 2


def _interrupted(signum, frame):
    raise ResultError('Collection interrupted; preserve local evidence')


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, _interrupted)
    raise SystemExit(main())
