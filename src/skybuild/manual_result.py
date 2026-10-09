"""Collect one pinned manual worker result; receipt is not review or acceptance."""
import argparse
from datetime import datetime
import hashlib
import json
import os
import stat
from pathlib import Path
import re
import subprocess
import sys
import time

from .client import Client, ClientError
from .contracts import valid_identifier
from .fleet_preflight import _resolved_addresses, _token_from_file
from .manual_assignment import AssignmentError, _path, verify_assignment
from .manual_cord import ManualCordError, _private_write
from .manual_dispatch import _private_endpoint


class ResultError(ValueError):
    pass


def receive_result(client, project, checkout, *, assignment, assignment_id, task_id,
                   worker, dispatcher, base_sha, message_id, destination,
                   approval_until, duration=120, max_pages=3,
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
            or not isinstance(scopes, list) or len(scopes) != 3
            or not all(isinstance(scope, str) for scope in scopes)
            or set(scopes) != {'cord:read', 'cord:send', 'cord:handle'}):
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
    key = 'manual-result-receipt-' + hashlib.sha256((project + '\n' + message_id).encode()).hexdigest()
    reply = call(client.message_action, project, message_id, 'receipt', idempotency_key=key)
    if not isinstance(reply, dict) or reply.get('message_id') != message_id or not reply.get('delivered_at'):
        raise ResultError("Result receipt is unconfirmed; preserve local evidence")
    return {'saved': str(destination), 'message_id': message_id, 'assignment_id': assignment_id,
            'task_id': task_id, 'head_sha': head, 'phase': result['phase'], 'receipted': True,
            'authority': 'markdown', 'review_required': True}


def _read_assignment(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 65536:
            raise ResultError("Assignment input must be a bounded ordinary file")
        data = os.read(descriptor, 65537)
        if len(data) > 65536:
            raise ResultError("Assignment input exceeds its bound")
        return json.loads(data)
    finally:
        os.close(descriptor)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('url', 'project', 'worker', 'dispatcher', 'assignment-id', 'task-id',
                 'base-sha', 'message-id', 'approval-until'):
        parser.add_argument('--' + name, required=True)
    for name in ('token-file', 'checkout', 'assignment', 'destination'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--ca-file', type=Path)
    parser.add_argument('--duration', type=int, default=120)
    parser.add_argument('--max-pages', type=int, default=3)
    args = parser.parse_args(argv)
    try:
        started = time.monotonic()
        if not 1 <= args.duration <= 120 or not 1 <= args.max_pages <= 10:
            raise ResultError("Collection bounds are invalid")
        # Validate cutoff before DNS, credentials or REST. receive_result repeats
        # it before every external call and before receipt.
        cutoff = datetime.fromisoformat(args.approval_until.replace('Z', '+00:00'))
        if cutoff.tzinfo is None or cutoff.timestamp() <= time.time():
            raise ResultError('Collection deadline expired')
        endpoint = _private_endpoint(args.url, _resolved_addresses)
        assignment = _read_assignment(args.assignment)
        duration = int(args.duration - (time.monotonic() - started))
        if duration < 1 or cutoff.timestamp() <= time.time():
            raise ResultError('Collection deadline expired')
        with Client(endpoint, _token_from_file(args.token_file), retries=0, timeout=5,
                    trust_env=False, ca_file=args.ca_file) as client:
            output = receive_result(client, args.project, args.checkout, assignment=assignment,
                assignment_id=args.assignment_id, task_id=args.task_id, worker=args.worker,
                dispatcher=args.dispatcher, base_sha=args.base_sha, message_id=args.message_id,
                destination=args.destination, approval_until=args.approval_until,
                duration=duration, max_pages=args.max_pages)
        print(json.dumps(output, sort_keys=True))
        return 0
    except (ResultError, AssignmentError, ManualCordError, ClientError, OSError, ValueError,
            TypeError, UnicodeError, subprocess.SubprocessError):
        print(json.dumps({'ok': False, 'reason': 'Result collection failed; preserve local evidence'}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
