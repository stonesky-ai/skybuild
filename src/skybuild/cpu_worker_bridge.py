"""One-shot local bridge for the reviewed deterministic CPU patch worker.

Only this trusted controller constructs argv. The worker receives private
assignment inputs and its scoped credentials by path; the owner token is never
an input to this adapter or copied into a worker directory.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
from uuid import uuid4

from scripts.skybuild_job_unit import JobSpec, JobUnitError, JobUnitManager, JobUnitState

from .contracts import valid_identifier
from . import cpu_worker_dispatch as _dispatch_module
from . import client as _client_module
from . import manual_cord as _manual_cord_module
from . import auto_patch_worker as _worker_module
from . import auto_patch_permit as _permit_module
from . import manual_assignment as _assignment_module
from . import manual_dispatch as _manual_dispatch_module
from . import fleet_preflight as _preflight_module
from .client import Client, ClientError


PROFILE = 'bounded-trusted-cpu-patch-v1'
WORKER_SOURCE = {
    'src/skybuild/__init__.py': '6dc62b0e7d135b949d66c97b99c12155a4ffa60d822b994b9a5d109b1ebe9af1',
    # Auto-worker client from 3bb plus this branch's CPU dispatch endpoints.
    'src/skybuild/client.py': '9a6ab69f0b3294e429375ada334d263d9df1b26878f11f928b98a5f024c9679e',
    'src/skybuild/fleet_preflight.py': 'f2ec5d39b6b1bc0c0a71354a7be89837bd153b5812a9be55f8a951a15fc9424c',
    'src/skybuild/auto_patch_worker.py': 'bf16d653051d9a4da12585b5291449144c402930c62477146f20999096e5318f',
    'src/skybuild/auto_patch_permit.py': 'de6cf3cbc6ca88f90c68bb397959cbf1c040feba1d355dd199bb142959f45eea',
    'src/skybuild/manual_assignment.py': '349dc9f367ba63e0bf6c2f3e2d63b7d45c6e5dadcb715f6b67295ad86b65daf7',
    'src/skybuild/manual_cord.py': '3cf3b393a18c39aa5c13dd19975211caba88171fd7be19025c24d47d9d59ca90',
    'src/skybuild/manual_dispatch.py': '7baad50262ad315c4d1d48cf6edb27c592b5369b23c3fb81004994a966a667cc',
}
SOURCE_DIGEST = hashlib.sha256(json.dumps(WORKER_SOURCE, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
_HEX64 = re.compile(r'[0-9a-f]{64}\Z')
_SAFE_ENV = ('PATH', 'LANG', 'LC_ALL', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS')
_PRIVATE_ASSIGNMENT_FILES = ('assignment.json', 'assignment.json.workflow.json.intent',
                             'assignment.json.workflow.json', 'preclaim.json')
_CONTROLLER_FILES = {
    'src/skybuild/__init__.py': sys.modules['skybuild'].__file__,
    'src/skybuild/auto_patch_controller.py': getattr(
        sys.modules.get('skybuild.auto_patch_controller'), '__file__',
        str(Path(__file__).with_name('auto_patch_controller.py'))),
    'src/skybuild/auto_patch_worker.py': _worker_module.__file__,
    'src/skybuild/auto_patch_permit.py': _permit_module.__file__,
    'src/skybuild/client.py': _client_module.__file__,
    'src/skybuild/fleet_preflight.py': _preflight_module.__file__,
    'src/skybuild/manual_assignment.py': _assignment_module.__file__,
    'src/skybuild/manual_dispatch.py': _manual_dispatch_module.__file__,
    'src/skybuild/manual_cord.py': _manual_cord_module.__file__,
    'src/skybuild/contracts.py': sys.modules['skybuild.contracts'].__file__,
    'src/skybuild/cpu_worker_bridge.py': __file__,
    'src/skybuild/cpu_worker_dispatch.py': _dispatch_module.__file__,
    'scripts/skybuild_job_unit.py': sys.modules[JobUnitManager.__module__].__file__,
}


class CPUWorkerBridgeError(ValueError):
    pass


@dataclass(frozen=True)
class CPUWorkerPlan:
    project_id: str
    worker_id: str
    dispatcher_id: str
    url: str
    checkout: Path
    assignment_dir: Path
    patch_file: Path
    patch_digest: str
    worker_token_file: Path
    git_token_file: Path
    ca_file: Path
    permit_file: Path
    permit_digest: str
    owner_token_file: Path
    weekly_usage_file: Path
    hostwatch_file: Path
    external_state_dir: Path
    controller_profile_file: Path


@dataclass(frozen=True)
class PreparedCPUWorker:
    plan: CPUWorkerPlan
    spec: JobSpec | None
    action_id: str
    operation_id: str
    assignment_id: str
    task_id: str
    attempt_id: str
    claim_fence: int
    approved_until: datetime
    source_head: str
    source_digest: str
    interpreter_digest: str
    controller_head: str
    controller_source_digest: str
    controller_profile_digest: str
    ca_digest: str
    owner_token_digest: str
    worker_token_digest: str
    git_token_digest: str
    assignment_digest: str
    argv_digest: str
    unit_name: str
    launch_nonce: str


def _file_bytes(path: Path, *, limit: int, private: bool, owner: bool = True,
                allow_empty: bool = False) -> bytes:
    if not path.is_absolute() or path.is_symlink():
        raise CPUWorkerBridgeError('Pinned input paths must be absolute non-symlinks')
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or (info.st_size <= 0 and not allow_empty)
                    or info.st_size > limit
                    or (private and (info.st_mode & 0o077))
                    or (owner and info.st_uid != os.geteuid())):
                raise CPUWorkerBridgeError('Pinned file is not a bounded private regular file')
            data = os.read(descriptor, limit + 1)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise CPUWorkerBridgeError(f'Pinned input unavailable: {error}') from None
    if len(data) > limit:
        raise CPUWorkerBridgeError('Pinned input exceeds its byte bound')
    return data


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _attempt_log_path(state_dir: Path, attempt_id: str) -> Path:
    return state_dir / ('worker-' + _digest(attempt_id.encode()) + '.log')


def _unit_manager(state_dir: Path) -> JobUnitManager:
    """Construct the production adapter; no caller-supplied process witness."""
    manager = JobUnitManager(state_dir)
    if type(manager) is not JobUnitManager or manager.run is not subprocess.run:
        raise CPUWorkerBridgeError('CPU bridge requires the pinned JobUnitManager and subprocess runner')
    return manager


def _trusted_client(plan: CPUWorkerPlan, expected_ca_digest: str | None = None,
                    expected_owner_token_digest: str | None = None) -> Client:
    """Build the owner-only API client from private operator configuration."""
    if not re.fullmatch(r'https://[^\s]+', plan.url):
        raise CPUWorkerBridgeError('Owner API endpoint must be HTTPS')
    owner_token_path = plan.owner_token_file.resolve(strict=True)
    if (owner_token_path.is_relative_to(plan.checkout.resolve(strict=True))
            or owner_token_path.is_relative_to(plan.external_state_dir.resolve(strict=True))):
        raise CPUWorkerBridgeError('Owner API token must remain outside source and worker state')
    token_bytes = _file_bytes(plan.owner_token_file, limit=4096, private=True)
    token_digest = _digest(token_bytes)
    if expected_owner_token_digest is not None and token_digest != expected_owner_token_digest:
        raise CPUWorkerBridgeError('Owner API token differs from the prepared private credential')
    try:
        token = token_bytes.decode('utf-8').removesuffix('\n')
    except UnicodeError:
        raise CPUWorkerBridgeError('Owner API token is not UTF-8') from None
    if not 32 <= len(token) <= 4096 or any(character.isspace() for character in token):
        raise CPUWorkerBridgeError('Owner API token file is malformed')
    ca_bytes = _file_bytes(plan.ca_file, limit=1_048_576, private=False, owner=False)
    ca_digest = _digest(ca_bytes)
    if expected_ca_digest is not None and ca_digest != expected_ca_digest:
        raise CPUWorkerBridgeError('Owner API CA differs from the prepared trust pin')
    client = None
    try:
        client = Client(plan.url, token, retries=0, timeout=10, trust_env=False,
                        ca_file=plan.ca_file, expected_ca_sha256=ca_digest)
        identity = client.whoami()
    except (ClientError, ValueError, OSError) as error:
        if client is not None:
            client.close()
        raise CPUWorkerBridgeError(f'Owner API client configuration failed: {type(error).__name__}') from None
    if not isinstance(identity, dict) or identity.get('is_admin') is not True:
        client.close()
        raise CPUWorkerBridgeError('CPU bridge owner credential must authenticate as an administrator')
    return client


def _trusted_worker_client(prepared: PreparedCPUWorker) -> Client:
    raw = _file_bytes(prepared.plan.worker_token_file, limit=1024, private=True)
    if _digest(raw) != prepared.worker_token_digest:
        raise CPUWorkerBridgeError('Worker token differs from its prepared private credential')
    try:
        token = raw.decode('utf-8').removesuffix('\n')
        client = Client(prepared.plan.url, token, retries=0, timeout=10, trust_env=False,
                        ca_file=prepared.plan.ca_file, expected_ca_sha256=prepared.ca_digest)
        identity = client.whoami()
    except (ClientError, UnicodeError, ValueError, OSError) as error:
        if 'client' in locals():
            client.close()
        raise CPUWorkerBridgeError(f'Worker API client configuration failed: {type(error).__name__}') from None
    grants = identity.get('grants', {}).get(prepared.plan.project_id, []) if isinstance(identity, dict) else []
    if (not isinstance(identity, dict) or identity.get('principal_id') != prepared.plan.worker_id
            or identity.get('is_admin') is not False
            or not {'tasks:read', 'tasks:claim'}.issubset(set(grants))):
        client.close()
        raise CPUWorkerBridgeError('Claim renewal requires the exact non-admin worker principal')
    return client


def _renew_worker_claim(client: Client, prepared: PreparedCPUWorker, stage: str) -> dict:
    """Renew exact worker fence with one durable idempotency key per boundary."""
    if stage not in {'pre-settle', 'pre-submit'}:
        raise CPUWorkerBridgeError('Claim renewal boundary is invalid')
    assignment, preclaim, workflow, digest = _read_assignment(prepared.plan, allow_runtime=True)
    if digest != prepared.assignment_digest:
        raise CPUWorkerBridgeError('Saved assignment inputs changed after dispatch preparation')
    token = workflow['token']
    view = client.task_workflow(prepared.plan.project_id, prepared.task_id)
    current = view.get('token') if isinstance(view, dict) else None
    task = view.get('task') if isinstance(view, dict) else None
    if (not isinstance(current, dict) or current.get('place') != 'working'
            or any(current.get(name) != token.get(name) for name in
                   ('project_id', 'task_id', 'attempt_id', 'claim_fence', 'input_generation',
                    'definition_revision', 'policy_version', 'source_head', 'target_base'))
            or not isinstance(task, dict) or type(task.get('revision')) is not int):
        raise CPUWorkerBridgeError('Original worker claim or task fence changed; preserve result')
    latest = _latest_claim_event(client, prepared.plan.project_id, prepared.task_id)
    if (not isinstance(latest, dict) or latest.get('action') not in {'claim', 'renew'}
            or not isinstance(latest.get('after_state'), dict)
            or latest['after_state'].get('held') is not True
            or latest['after_state'].get('holder') != prepared.plan.worker_id
            or latest['after_state'].get('fence') != prepared.claim_fence):
        raise CPUWorkerBridgeError('Original worker claim is not current; preserve result')
    key = 'cpu-result-' + _digest((prepared.operation_id + ':' + stage).encode())
    intent = {'schema': 'skybuild.cpu-claim-renewal.v1', 'operation_id': prepared.operation_id,
              'project_id': prepared.plan.project_id, 'task_id': prepared.task_id,
              'attempt_id': prepared.attempt_id, 'claim_fence': prepared.claim_fence,
              'expected_revision': task['revision'], 'lease_seconds': 300,
              'idempotency_key': key, 'stage': stage}
    path = prepared.plan.assignment_dir / ('claim-renewal-' + stage + '.json')
    if path.exists() or path.is_symlink():
        try:
            saved = json.loads(_file_bytes(path, limit=4096, private=True))
        except (CPUWorkerBridgeError, ValueError, UnicodeError):
            raise CPUWorkerBridgeError('Saved claim renewal intent is invalid; preserve result') from None
        if saved != intent:
            raise CPUWorkerBridgeError('Saved claim renewal intent binds another fence')
    else:
        _write_exclusive(path, (json.dumps(intent, sort_keys=True) + '\n').encode())
    response = client.request(
        'POST', Client._path(prepared.plan.project_id,
                             'tasks/' + Client._segment(prepared.task_id) + '/claim/renew'),
        body={'fence': prepared.claim_fence, 'lease_seconds': 300},
        revision=task['revision'], idempotency_key=key)
    if (not isinstance(response, dict) or response.get('fence') != prepared.claim_fence
            or response.get('holder') != prepared.plan.worker_id or response.get('held') is not True):
        raise CPUWorkerBridgeError('Worker claim renewal is unconfirmed; preserve result')
    return response


def _history_pages(client: Client, project_id: str, task_id: str, *, claim: bool):
    """Read ascending history to its actual tail; page bounds fail closed."""
    path = Client._path(project_id, 'tasks/' + Client._segment(task_id) +
                        ('/claim/history' if claim else '/history'))
    latest = None
    limit = 100
    for page_number in range(1000):
        page = client.request('GET', path, params={'limit': limit, 'offset': page_number * limit})
        if not isinstance(page, list) or len(page) > limit:
            raise CPUWorkerBridgeError('Task history page is malformed; preserve result')
        if page:
            latest = page[-1]
        if len(page) < limit:
            return latest
    raise CPUWorkerBridgeError('Task history exceeds the bounded reconciliation scan')


def _latest_claim_event(client: Client, project_id: str, task_id: str) -> dict | None:
    return _history_pages(client, project_id, task_id, claim=True)


def _history_has_submit(client: Client, project_id: str, task_id: str,
                        expected_receipt: dict) -> bool:
    path = Client._path(project_id, 'tasks/' + Client._segment(task_id) + '/history')
    limit = 100
    for page_number in range(1000):
        page = client.request('GET', path, params={'limit': limit, 'offset': page_number * limit})
        if not isinstance(page, list) or len(page) > limit:
            raise CPUWorkerBridgeError('Task history page is malformed; preserve submit intent')
        if any(isinstance(item, dict) and item.get('operation') == 'workflow.submit'
               and isinstance(item.get('event_facts'), dict)
               and item['event_facts'].get('author_output_receipt') == expected_receipt
               for item in page):
            return True
        if len(page) < limit:
            return False
    raise CPUWorkerBridgeError('Task history exceeds the bounded submit reconciliation scan')


def _result_intent(prepared: PreparedCPUWorker, assignment: dict, preclaim: dict,
                   workflow: dict) -> tuple[dict, bytes]:
    path = prepared.plan.assignment_dir / 'result-intent.json'
    raw = _file_bytes(path, limit=65536, private=True)
    try:
        intent = json.loads(raw)
    except (ValueError, UnicodeError):
        raise CPUWorkerBridgeError('Worker result intent is malformed; preserve exposure') from None
    token = workflow.get('token')
    if not isinstance(token, dict):
        raise CPUWorkerBridgeError('Saved workflow token is unavailable')
    from .manual_cord import result_message
    result = intent.get('result') if isinstance(intent, dict) else None
    if not isinstance(result, dict):
        raise CPUWorkerBridgeError('Worker result intent has no exact result envelope')
    try:
        message, key = result_message(assignment, result, relay_worker=prepared.plan.worker_id)
    except (KeyError, ValueError, TypeError):
        raise CPUWorkerBridgeError('Worker result envelope cannot be reconstructed') from None
    expected = {
        'schema': 'skybuild.cpu-result-intent.v1',
        'project_id': prepared.plan.project_id,
        'task_id': prepared.task_id,
        'assignment_id': prepared.assignment_id,
        'worker': prepared.plan.worker_id,
        'attempt_id': token['attempt_id'],
        'claim_fence': token['claim_fence'],
        'input_generation': token['input_generation'],
        'definition_revision': token['definition_revision'],
        'policy_version': token['policy_version'],
        'assignment_sha256': _digest(json.dumps(assignment, sort_keys=True, separators=(',', ':')).encode()),
        'brief_sha256': assignment['brief_sha256'],
        'patch_sha256': prepared.plan.patch_digest,
        'source_head': result.get('head_sha'),
        'source_branch': 'refs/heads/' + assignment['branch'],
        'target_base': assignment['base_sha'],
        'result': result,
        'message': message,
        'message_idempotency_key': key,
    }
    if (intent != expected or preclaim.get('attempt_id') != prepared.attempt_id
            or preclaim.get('claim_fence') != prepared.claim_fence
            or result.get('schema') != 'manual-work-v1'
            or result.get('phase') != 'ready-for-review'
            or not isinstance(result.get('head_sha'), str)
            or not re.fullmatch(r'[0-9a-f]{40}', result['head_sha'])):
        raise CPUWorkerBridgeError('Result intent differs from exact assignment or worker fence')
    return intent, raw


def _verify_pushed_result(prepared: PreparedCPUWorker, intent: dict) -> None:
    worktree = prepared.plan.assignment_dir / 'source'
    result = intent['result']
    askpass = prepared.plan.assignment_dir / 'git-askpass.sh'
    expected_askpass = (b'#!/bin/sh\ncase "$1" in\n'
                        b'  *Username*) printf \'%s\\n\' \'x-access-token\' ;;\n'
                        b'  *Password*) exec /bin/cat -- "$SKYBUILD_GIT_TOKEN_FILE" ;;\n'
                        b'  *) exit 1 ;;\nesac\n')
    if _file_bytes(askpass, limit=2048, private=True) != expected_askpass:
        raise CPUWorkerBridgeError('Trusted Git credential helper differs; preserve exposure')
    git_token = _file_bytes(prepared.plan.git_token_file, limit=1024, private=True)
    if _digest(git_token) != prepared.git_token_digest:
        raise CPUWorkerBridgeError('Git credential differs from prepared private reference')
    origin = subprocess.run(['git', 'remote', 'get-url', 'origin'], cwd=worktree,
                            capture_output=True, text=True, timeout=5, check=False)
    branch = subprocess.run(['git', 'symbolic-ref', '--short', 'HEAD'], cwd=worktree,
                            capture_output=True, text=True, timeout=5, check=False)
    head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=worktree,
                         capture_output=True, text=True, timeout=5, check=False)
    status = subprocess.run(['git', 'status', '--porcelain=v1', '--untracked-files=all'], cwd=worktree,
                            capture_output=True, text=True, timeout=5, check=False)
    if (origin.returncode or origin.stdout.strip() != 'https://github.com/stonesky-ai/skybuild.git'
            or branch.returncode or branch.stdout.strip() != result['branch']
            or head.returncode or head.stdout.strip() != result['head_sha']
            or status.returncode or status.stdout):
        raise CPUWorkerBridgeError('Local pushed result snapshot differs; preserve exposure')
    environment = {key: os.environ[key] for key in _SAFE_ENV if key in os.environ}
    environment.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null',
                       GIT_TERMINAL_PROMPT='0', GIT_ASKPASS=str(askpass),
                       SKYBUILD_GIT_TOKEN_FILE=str(prepared.plan.git_token_file))
    remote = subprocess.run(['git', 'ls-remote', '--refs', 'origin', intent['source_branch']],
                            cwd=worktree, env=environment, capture_output=True, text=True,
                            timeout=30, check=False)
    if remote.returncode or remote.stdout.strip() != result['head_sha'] + '\t' + intent['source_branch']:
        raise CPUWorkerBridgeError('Authenticated remote head does not match result intent; preserve exposure')


def _write_exclusive(path: Path, data: bytes, mode: int = 0o600) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _source_head(checkout: Path) -> str:
    if any(key.startswith('GIT_') and key != 'GIT_PAGER' for key in os.environ):
        raise CPUWorkerBridgeError('Inherited Git environment blocks profile verification')
    env = {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL') if key in os.environ}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null', GIT_TERMINAL_PROMPT='0')
    try:
        head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=checkout, env=env,
                              capture_output=True, text=True, timeout=5, check=False)
        status = subprocess.run(['git', 'status', '--porcelain=v1', '--untracked-files=all', '-z'],
                                cwd=checkout, env=env, capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        raise CPUWorkerBridgeError('Trusted source checkout cannot be verified') from None
    if (head.returncode or status.returncode or status.stdout
            or not re.fullmatch(r'[0-9a-f]{40}', head.stdout.strip())):
        raise CPUWorkerBridgeError('Trusted source checkout must be clean and pinned')
    for relative, expected in WORKER_SOURCE.items():
        path = checkout / relative
        if path.is_symlink() or not path.is_file() or _digest(path.read_bytes()) != expected:
            raise CPUWorkerBridgeError('Installed worker source differs from reviewed static profile')
    return head.stdout.strip()


def _controller_pin(plan: CPUWorkerPlan, interpreter_digest: str) -> tuple[str, str, str]:
    """Compare loaded authority modules to an owner-configured exact profile."""
    root = Path(__file__).resolve().parents[2]
    if plan.checkout.resolve(strict=True) != root:
        raise CPUWorkerBridgeError('Worker import checkout must be the exact controller source root')
    entrypoint = (sys.modules.get('skybuild.auto_patch_controller')
                  or sys.modules.get('__main__'))
    if (entrypoint is None or not getattr(entrypoint, '__file__', None)
            or Path(entrypoint.__file__).resolve(strict=True) != root / 'src/skybuild/auto_patch_controller.py'):
        raise CPUWorkerBridgeError('Trusted CPU bridge must run under the pinned automatic controller entrypoint')
    if plan.controller_profile_file.is_symlink():
        raise CPUWorkerBridgeError('Trusted controller profile cannot be a symlink')
    profile_path = plan.controller_profile_file.resolve(strict=True)
    if profile_path.is_relative_to(plan.checkout.resolve()) or profile_path.is_relative_to(root):
        raise CPUWorkerBridgeError('Trusted controller profile must be outside source checkouts')
    raw_profile = _file_bytes(profile_path, limit=32768, private=True)
    try:
        profile = json.loads(raw_profile)
    except (ValueError, UnicodeError):
        raise CPUWorkerBridgeError('Trusted controller profile is malformed') from None
    if not isinstance(profile, dict) or set(profile) != {
            'schema', 'profile_id', 'project_id', 'api_url', 'controller_head',
            'controller_files', 'interpreter_sha256'}:
        raise CPUWorkerBridgeError('Trusted controller profile fields differ')
    env = {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL') if key in os.environ}
    if any(key.startswith('GIT_') and key != 'GIT_PAGER' for key in os.environ):
        raise CPUWorkerBridgeError('Inherited Git environment blocks controller verification')
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null', GIT_TERMINAL_PROMPT='0')
    try:
        head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root, env=env,
                              capture_output=True, text=True, timeout=5, check=False)
        status = subprocess.run(['git', 'status', '--porcelain=v1', '--untracked-files=all', '-z'],
                                cwd=root, env=env, capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        raise CPUWorkerBridgeError('Controller source checkout cannot be verified') from None
    if (head.returncode or status.returncode or status.stdout
            or not re.fullmatch(r'[0-9a-f]{40}', head.stdout.strip())):
        raise CPUWorkerBridgeError('Controller source checkout must be clean and pinned')
    file_digests = {}
    for relative, origin in _CONTROLLER_FILES.items():
        expected_path = (root / relative).resolve(strict=True)
        if Path(origin).resolve(strict=True) != expected_path:
            raise CPUWorkerBridgeError('Loaded controller module origin differs from its trusted checkout')
        try:
            blob = subprocess.run(['git', 'show', 'HEAD:' + relative], cwd=root, env=env,
                                  capture_output=True, timeout=5, check=False)
        except (OSError, subprocess.SubprocessError):
            raise CPUWorkerBridgeError('Controller source blob is unavailable') from None
        current = _file_bytes(expected_path, limit=4 * 1024 * 1024, private=False, owner=False)
        if blob.returncode or blob.stdout != current:
            raise CPUWorkerBridgeError('Loaded controller bytes differ from its pinned Git head')
        file_digests[relative] = _digest(current)
    if (profile.get('schema') != 'skybuild.cpu-worker-controller-profile.v1'
            or profile.get('profile_id') != PROFILE
            or profile.get('project_id') != plan.project_id
            or profile.get('api_url') != plan.url
            or profile.get('controller_head') != head.stdout.strip()
            or profile.get('controller_files') != file_digests
            or profile.get('interpreter_sha256') != interpreter_digest):
        raise CPUWorkerBridgeError('Controller source or interpreter is not in the owner profile allowlist')
    controller_source_digest = _digest(json.dumps(
        {'controller_head': head.stdout.strip(), 'controller_files': file_digests},
        sort_keys=True, separators=(',', ':')).encode())
    return head.stdout.strip(), controller_source_digest, _digest(raw_profile)


def _private_dir(path: Path) -> None:
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise CPUWorkerBridgeError('Worker state path must be an existing absolute directory')
    info = path.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise CPUWorkerBridgeError('Worker state directory must be owned and mode 0700')


def _read_assignment(plan: CPUWorkerPlan, *, allow_runtime: bool = False) -> tuple[dict, dict, dict, str]:
    _private_dir(plan.assignment_dir)
    if plan.assignment_dir.resolve().is_relative_to(plan.checkout.resolve()):
        raise CPUWorkerBridgeError('Worker state directory must be outside the source checkout')
    names = {item.name for item in plan.assignment_dir.iterdir()}
    allowed_runtime = {'intent.json', 'push-intent.json', 'result-intent.json', 'source',
                       'git-askpass.sh', 'observation-intent.json', 'submitted.json',
                       'assignment.json.workflow.json.submit', 'claim-renewal-pre-settle.json',
                       'claim-renewal-pre-submit.json'}
    allowed_runtime.update(name for name in names if re.fullmatch(r'observation-[0-9a-f-]{36}\.json', name))
    if (not set(_PRIVATE_ASSIGNMENT_FILES).issubset(names)
            or (not allow_runtime and names != set(_PRIVATE_ASSIGNMENT_FILES))
            or (allow_runtime and names - set(_PRIVATE_ASSIGNMENT_FILES) - allowed_runtime)):
        raise CPUWorkerBridgeError('Worker directory must contain exactly one pinned preclaim')
    blobs = {name: _file_bytes(plan.assignment_dir / name, limit=32768, private=True)
             for name in _PRIVATE_ASSIGNMENT_FILES}
    try:
        assignment = json.loads(blobs['assignment.json'])
        preclaim = json.loads(blobs['preclaim.json'])
        workflow = json.loads(blobs['assignment.json.workflow.json'])
    except (ValueError, UnicodeError):
        raise CPUWorkerBridgeError('Assignment snapshot is malformed') from None
    if (not isinstance(assignment, dict) or not isinstance(preclaim, dict)
            or not isinstance(workflow, dict) or assignment.get('worker') != plan.worker_id
            or assignment.get('dispatcher') != plan.dispatcher_id
            or preclaim.get('assignment_id') != assignment.get('assignment_id')
            or preclaim.get('task_id') != assignment.get('task_id')
            or preclaim.get('place') != 'working'
            or not isinstance(preclaim.get('attempt_id'), str)
            or type(preclaim.get('claim_fence')) is not int or preclaim['claim_fence'] < 1):
        raise CPUWorkerBridgeError('Assignment or fenced preclaim identity differs')
    workflow_data = workflow.get('token')
    if (not isinstance(workflow_data, dict)
            or workflow_data.get('attempt_id') != preclaim['attempt_id']
            or workflow_data.get('claim_fence') != preclaim['claim_fence']
            or workflow_data.get('place') != 'working'):
        raise CPUWorkerBridgeError('Saved Petri claim differs from exact preclaim')
    digest_map = {name: _digest(blobs[name]) for name in sorted(blobs)}
    assignment_digest = _digest(json.dumps(digest_map, sort_keys=True, separators=(',', ':')).encode())
    return assignment, preclaim, workflow, assignment_digest


def _validate_permit(plan: CPUWorkerPlan, assignment: dict, assignment_digest: str, *,
                     allow_expired: bool = False, check_admission: bool = True) -> tuple[dict, datetime]:
    raw = _file_bytes(plan.permit_file, limit=16384, private=True)
    if _digest(raw) != plan.permit_digest:
        raise CPUWorkerBridgeError('Worker permit bytes differ from approved digest')
    try:
        permit = json.loads(raw)
        approved_until = datetime.fromisoformat(permit['approved_until'].replace('Z', '+00:00'))
    except (ValueError, TypeError, KeyError, AttributeError):
        raise CPUWorkerBridgeError('Worker permit is malformed') from None
    if (not isinstance(permit, dict) or permit.get('schema') != 'skybuild.auto-cpu-patch-permit.v1'
            or permit.get('profile') != PROFILE or permit.get('project_id') != plan.project_id
            or permit.get('host_id') != socket.gethostname() or permit.get('slots') != 2
            or permit.get('source_head') != _source_head(plan.checkout)
            or approved_until.tzinfo is None
            or (not allow_expired and approved_until <= datetime.now(timezone.utc))):
        raise CPUWorkerBridgeError('Worker permit scope or source pin is not current')
    selected = permit.get('workers')
    if not isinstance(selected, list) or not any(
            item.get('task_id') == assignment.get('task_id')
            and item.get('assignment_id') == assignment.get('assignment_id')
            and item.get('worker') == plan.worker_id
            and item.get('patch_sha256') == plan.patch_digest
            for item in selected if isinstance(item, dict)):
        raise CPUWorkerBridgeError('Permit does not pin this exact task and patch')
    if assignment_digest == '':
        raise CPUWorkerBridgeError('Assignment digest is unavailable')
    for name in ('hostwatch_reserve_bytes', 'memory_high_bytes', 'memory_max_bytes', 'runtime_seconds'):
        if type(permit.get(name)) is not int or permit[name] <= 0:
            raise CPUWorkerBridgeError('Worker permit resource bound is invalid')
    if (permit['hostwatch_reserve_bytes'] < 8 * 1024**3
            or not permit['memory_high_bytes'] < permit['memory_max_bytes'] <= 4 * 1024**3
            or permit['runtime_seconds'] > 1800):
        raise CPUWorkerBridgeError('Worker permit exceeds the approved CPU resource profile')
    if check_admission:
        try:
            from .auto_patch_permit import check_weekly_usage, resource_admission
            check_weekly_usage(plan.weekly_usage_file, permit)
            resource_admission(plan.hostwatch_file, permit, selected_count=2)
        except (ImportError, ValueError, OSError) as error:
            raise CPUWorkerBridgeError(f'Host or weekly CPU admission failed: {type(error).__name__}') from None
    return permit, approved_until


def _worker_interpreter(checkout: Path) -> tuple[Path, str]:
    """Keep virtual-environment identity while pinning the executable bytes."""
    interpreter = Path(sys.executable).absolute()
    executable = interpreter.resolve(strict=True)
    if not stat.S_ISREG(executable.stat().st_mode) or not os.access(interpreter, os.X_OK):
        raise CPUWorkerBridgeError('Trusted Python interpreter is unavailable')
    digest = _digest(_file_bytes(executable, limit=256 * 1024 * 1024,
                                 private=False, owner=False))
    probe = subprocess.run(
        [str(interpreter), '-I', '-c', 'import skybuild; print(skybuild.__file__)'],
        cwd=checkout, env={key: os.environ[key] for key in _SAFE_ENV if key in os.environ},
        capture_output=True, timeout=5, check=False)
    if (probe.returncode or probe.stdout.decode().strip() != str(checkout / 'src/skybuild/__init__.py')):
        raise CPUWorkerBridgeError('Worker interpreter does not import the approved checkout')
    return interpreter, digest


def prepare_worker(plan: CPUWorkerPlan, *, action_id: str, operation_id: str) -> PreparedCPUWorker:
    """Validate immutable inputs, prepare API intent, then return one fixed JobSpec."""
    for name, value in [('project_id', plan.project_id), ('worker_id', plan.worker_id),
                        ('dispatcher_id', plan.dispatcher_id), ('action_id', action_id),
                        ('operation_id', operation_id)]:
        if not valid_identifier(value):
            raise CPUWorkerBridgeError(f'{name} is not a stable identifier')
    if plan.checkout.is_symlink():
        raise CPUWorkerBridgeError('Trusted checkout cannot be a symlink')
    checkout = plan.checkout.resolve(strict=True)
    if not checkout.is_dir():
        raise CPUWorkerBridgeError('Trusted checkout must be a real directory')
    if checkout != Path(__file__).resolve().parents[2]:
        raise CPUWorkerBridgeError('Worker import checkout must be the exact controller source root')
    source_head = _source_head(checkout)
    assignment, preclaim, _, assignment_digest = _read_assignment(plan)
    patch = _file_bytes(plan.patch_file, limit=65536, private=True)
    if _digest(patch) != plan.patch_digest:
        raise CPUWorkerBridgeError('Approved patch bytes differ from pinned digest')
    for path in (plan.worker_token_file, plan.git_token_file):
        _file_bytes(path, limit=1024, private=True)
    owner_token_bytes = _file_bytes(plan.owner_token_file, limit=4096, private=True)
    worker_token_bytes = _file_bytes(plan.worker_token_file, limit=1024, private=True)
    git_token_bytes = _file_bytes(plan.git_token_file, limit=1024, private=True)
    secret_paths = {path.resolve(strict=True) for path in
                    (plan.owner_token_file, plan.worker_token_file, plan.git_token_file)}
    if len(secret_paths) != 3 or len({owner_token_bytes, worker_token_bytes, git_token_bytes}) != 3:
        raise CPUWorkerBridgeError('Owner, worker and Git credentials must be distinct private files')
    owner_token_path = plan.owner_token_file.resolve(strict=True)
    if (owner_token_path.is_relative_to(checkout)
            or owner_token_path.is_relative_to(plan.external_state_dir.resolve(strict=True))):
        raise CPUWorkerBridgeError('Owner API token must remain outside source and worker state')
    owner_token_digest = _digest(owner_token_bytes)
    ca_bytes = _file_bytes(plan.ca_file, limit=1_048_576, private=False, owner=False)
    ca_digest = _digest(ca_bytes)
    permit, approved_until = _validate_permit(plan, assignment, assignment_digest)
    interpreter, interpreter_digest = _worker_interpreter(checkout)
    controller_head, controller_source_digest, controller_profile_digest = _controller_pin(
        plan, interpreter_digest)
    if not re.fullmatch(r'https://[^\s]+', plan.url):
        raise CPUWorkerBridgeError('Worker endpoint must be HTTPS')
    if not plan.external_state_dir.is_absolute() or plan.external_state_dir.resolve().is_relative_to(checkout):
        raise CPUWorkerBridgeError('Worker state must remain outside the checkout')
    _private_dir(plan.external_state_dir)
    worker_state = plan.assignment_dir
    if worker_state.parent.resolve() != plan.external_state_dir.resolve():
        raise CPUWorkerBridgeError('Assignment snapshot must be a direct private state child')
    attempt_id = preclaim['attempt_id']
    stdin_path = plan.external_state_dir / 'stdin.empty'
    if not stdin_path.exists():
        _write_exclusive(stdin_path, b'')
    _file_bytes(stdin_path, limit=1, private=True, allow_empty=True)
    log_path = _attempt_log_path(plan.external_state_dir, attempt_id)
    if not log_path.exists():
        _write_exclusive(log_path, b'')
    if log_path.is_symlink() or not log_path.is_file() or log_path.stat().st_uid != os.geteuid() or log_path.stat().st_mode & 0o077:
        raise CPUWorkerBridgeError('Worker log must be a private regular file')
    worker_id = plan.worker_id
    if not isinstance(preclaim.get('message_id'), str) or not preclaim['message_id']:
        raise CPUWorkerBridgeError('Fenced preclaim has no message identity')
    args = (str(interpreter), '-I', '-m', 'skybuild.auto_patch_worker', '--url', plan.url,
            '--project', plan.project_id, '--worker', worker_id, '--dispatcher', plan.dispatcher_id,
            '--message-id', preclaim.get('message_id'), '--checkout', str(checkout),
            '--token-file', str(plan.worker_token_file), '--git-token-file', str(plan.git_token_file),
            '--ca-file', str(plan.ca_file), '--patch', str(plan.patch_file),
            '--patch-sha256', plan.patch_digest, '--state-dir', str(worker_state),
            '--approved-until', permit['approved_until'], '--permit', str(plan.permit_file),
            '--permit-sha256', plan.permit_digest)
    argv_digest = _digest(json.dumps({'argv': args, 'interpreter_sha256': interpreter_digest,
                                      'ca_sha256': ca_digest, 'source_head': source_head},
                                     sort_keys=True, separators=(',', ':')).encode())
    task_id = assignment.get('task_id')
    remaining_seconds = int((approved_until - datetime.now(timezone.utc)).total_seconds())
    if remaining_seconds < 2:
        raise CPUWorkerBridgeError('Approval interval is too short for a bounded worker launch')
    spec = JobSpec(task_id=task_id, attempt_id=attempt_id, worktree=checkout, argv=args,
                   stdin_path=stdin_path, log_path=log_path,
                   memory_high_bytes=permit['memory_high_bytes'], memory_max_bytes=permit['memory_max_bytes'],
                   runtime_seconds=min(permit['runtime_seconds'], remaining_seconds - 1),
                   environment={key: os.environ[key] for key in _SAFE_ENV if key in os.environ})
    spec.validate()
    unit_name = spec.unit()
    launch_nonce = os.urandom(16).hex()
    request = {'action_id': action_id, 'operation_id': operation_id, 'profile_id': PROFILE,
               'host_id': socket.gethostname(), 'worker_id': worker_id, 'unit_name': unit_name,
               'launch_nonce': launch_nonce, 'source_digest': SOURCE_DIGEST,
               'controller_head': controller_head,
               'controller_source_digest': controller_source_digest,
               'controller_profile_digest': controller_profile_digest,
               'interpreter_digest': interpreter_digest,
               'permit_digest': plan.permit_digest, 'assignment_digest': assignment_digest,
               'patch_digest': plan.patch_digest, 'argv_digest': argv_digest,
               'approved_until': permit['approved_until']}
    with _trusted_client(plan, ca_digest, owner_token_digest) as client:
        response = client.prepare_cpu_worker_dispatch(plan.project_id, request)
    if (response.get('operation_id') != operation_id or response.get('action_id') != action_id
            or response.get('unit_name') != unit_name or response.get('launch_nonce') != launch_nonce
            or response.get('source_digest') != SOURCE_DIGEST
            or response.get('controller_head') != controller_head
            or response.get('controller_source_digest') != controller_source_digest
            or response.get('controller_profile_digest') != controller_profile_digest
            or response.get('interpreter_digest') != interpreter_digest):
        raise CPUWorkerBridgeError('API prepared a different worker invocation')
    pinned_spec = JobSpec(**{**spec.__dict__, 'launch_nonce': launch_nonce})
    return PreparedCPUWorker(plan, pinned_spec, action_id, operation_id,
                             assignment['assignment_id'], task_id, attempt_id,
                             preclaim['claim_fence'], approved_until, source_head, SOURCE_DIGEST,
                             interpreter_digest, controller_head, controller_source_digest,
                             controller_profile_digest, ca_digest,
                             owner_token_digest, _digest(worker_token_bytes),
                             _digest(git_token_bytes),
                             assignment_digest, argv_digest, unit_name, launch_nonce)


def recover_worker(plan: CPUWorkerPlan, journal: dict) -> PreparedCPUWorker:
    """Reconstruct a reconcile-only handle from durable local and API pins.

    This path never calls prepare, begin, or systemd start. A missing, changed,
    or ambiguous pin fails closed and leaves the existing exposure held.
    """
    required = {'schema', 'task_id', 'worker', 'assignment_id', 'attempt_id', 'claim_fence',
                'action_id', 'operation_id', 'unit', 'launch_nonce', 'approved_until',
                'source_head', 'source_digest', 'interpreter_digest', 'controller_head',
                'controller_source_digest', 'controller_profile_digest', 'ca_digest',
                'owner_token_digest', 'worker_token_digest', 'git_token_digest',
                'assignment_digest', 'argv_digest', 'phase', 'assignment_dir'}
    if not isinstance(journal, dict) or set(journal) != required or journal.get('schema') != 'skybuild.cpu-worker-launch.v1':
        raise CPUWorkerBridgeError('Durable launch journal is missing or has unknown fields')
    if (journal['task_id'] == '' or journal['worker'] != plan.worker_id
            or journal['assignment_dir'] != str(plan.assignment_dir)
            or journal['phase'] not in {'launch_intent', 'running', 'unknown', 'completed'}):
        raise CPUWorkerBridgeError('Durable launch journal differs from the supplied recovery plan')
    assignment, preclaim, _, assignment_digest = _read_assignment(plan, allow_runtime=True)
    if (assignment_digest != journal['assignment_digest']
            or assignment.get('task_id') != journal['task_id']
            or assignment.get('assignment_id') != journal['assignment_id']
            or preclaim.get('attempt_id') != journal['attempt_id']
            or preclaim.get('claim_fence') != journal['claim_fence']):
        raise CPUWorkerBridgeError('Durable assignment or claim differs from launch journal')
    source_head = _source_head(plan.checkout)
    interpreter = Path(sys.executable).resolve(strict=True)
    interpreter_digest = _digest(_file_bytes(interpreter, limit=256 * 1024 * 1024,
                                             private=False, owner=False))
    controller_head, controller_source_digest, controller_profile_digest = _controller_pin(
        plan, interpreter_digest)
    permit, approved_until = _validate_permit(plan, assignment, assignment_digest,
                                              allow_expired=True, check_admission=False)
    ca_digest = _digest(_file_bytes(plan.ca_file, limit=1_048_576, private=False, owner=False))
    owner_token_digest = _digest(_file_bytes(plan.owner_token_file, limit=4096, private=True))
    worker_token_digest = _digest(_file_bytes(plan.worker_token_file, limit=1024, private=True))
    git_token_digest = _digest(_file_bytes(plan.git_token_file, limit=1024, private=True))
    local_pins = {
        'source_head': source_head, 'source_digest': SOURCE_DIGEST,
        'interpreter_digest': interpreter_digest, 'controller_head': controller_head,
        'controller_source_digest': controller_source_digest,
        'controller_profile_digest': controller_profile_digest, 'ca_digest': ca_digest,
        'owner_token_digest': owner_token_digest, 'worker_token_digest': worker_token_digest,
        'git_token_digest': git_token_digest, 'assignment_digest': assignment_digest,
    }
    if any(journal.get(name) != value for name, value in local_pins.items()):
        raise CPUWorkerBridgeError('Local recovery pins differ from the durable launch journal')
    try:
        saved_until = datetime.fromisoformat(journal['approved_until'].replace('Z', '+00:00'))
    except (AttributeError, ValueError):
        raise CPUWorkerBridgeError('Durable approval cutoff is malformed') from None
    if saved_until.tzinfo is None or saved_until.astimezone(timezone.utc) != approved_until:
        raise CPUWorkerBridgeError('Durable approval cutoff differs from the pinned permit')
    with _trusted_client(plan, ca_digest, owner_token_digest) as owner:
        remote = owner.get_cpu_worker_dispatch(plan.project_id, journal['operation_id'])
    api_fields = {
        'operation_id': journal['operation_id'], 'action_id': journal['action_id'],
        'project_id': plan.project_id, 'task_id': journal['task_id'],
        'worker_id': plan.worker_id, 'attempt_id': journal['attempt_id'],
        'claim_fence': journal['claim_fence'], 'unit_name': journal['unit'],
        'launch_nonce': journal['launch_nonce'], 'source_digest': SOURCE_DIGEST,
        'controller_head': controller_head, 'controller_source_digest': controller_source_digest,
        'controller_profile_digest': controller_profile_digest,
        'interpreter_digest': interpreter_digest, 'permit_digest': plan.permit_digest,
        'assignment_digest': assignment_digest, 'patch_digest': plan.patch_digest,
        'argv_digest': journal['argv_digest'], 'host_id': socket.gethostname(),
    }
    if (not isinstance(remote, dict)
            or remote.get('state') not in {'prepared', 'launch-intent', 'running', 'unknown', 'terminal', 'settled'}
            or any(remote.get(name) != value for name, value in api_fields.items())):
        raise CPUWorkerBridgeError('API dispatch identity differs from durable launch journal')
    return PreparedCPUWorker(
        plan, None, journal['action_id'], journal['operation_id'], journal['assignment_id'],
        journal['task_id'], journal['attempt_id'], journal['claim_fence'], approved_until,
        source_head, SOURCE_DIGEST, interpreter_digest, controller_head,
        controller_source_digest, controller_profile_digest, ca_digest, owner_token_digest,
        worker_token_digest, git_token_digest, assignment_digest, journal['argv_digest'],
        journal['unit'], journal['launch_nonce'])


def _submit_after_settlement(owner: Client, worker_client: Client,
                             prepared: PreparedCPUWorker, intent: dict) -> dict:
    assignment, _, workflow, digest = _read_assignment(prepared.plan, allow_runtime=True)
    if digest != prepared.assignment_digest:
        raise CPUWorkerBridgeError('Saved assignment inputs changed after dispatch preparation')
    result = intent['result']
    workflow_path = prepared.plan.assignment_dir / 'assignment.json.workflow.json'
    submit_path = workflow_path.with_name(workflow_path.name + '.submit')
    current = owner.task_workflow(prepared.plan.project_id, prepared.task_id)
    token = current.get('token') if isinstance(current, dict) else None
    saved_token = workflow.get('token')
    stable_fields = ('project_id', 'task_id', 'attempt_id', 'claim_fence',
                     'definition_revision', 'policy_version')
    if (not isinstance(token, dict) or not isinstance(saved_token, dict)
            or any(token.get(name) != saved_token.get(name) for name in stable_fields)):
        return {'submitted': False, 'settled': True,
                'reason': 'task fence changed after settlement; preserve exact result for owner reconciliation'}
    expected_receipt = {name: saved_token[name] for name in
                        ('attempt_id', 'claim_fence', 'input_generation',
                         'definition_revision', 'policy_version')}
    expected_receipt.update(source_head=result['head_sha'],
                            source_branch=intent['source_branch'],
                            target_base=intent['target_base'])
    saved_submit = None
    if submit_path.exists() or submit_path.is_symlink():
        try:
            saved_submit = json.loads(_file_bytes(submit_path, limit=8192, private=True))
        except (CPUWorkerBridgeError, ValueError, UnicodeError):
            raise CPUWorkerBridgeError('Saved owner submission intent is invalid; preserve settled result') from None
        if saved_submit.get('body') != expected_receipt or saved_submit.get('idempotency_key') != intent['message_idempotency_key']:
            raise CPUWorkerBridgeError('Saved owner submission intent differs; preserve settled result')
    if token.get('place') == 'working':
        if any(token.get(name) != saved_token.get(name) for name in
               ('input_generation', 'source_head', 'target_base')):
            return {'submitted': False, 'settled': True,
                    'reason': 'working task inputs changed after settlement; preserve result'}
        try:
            _renew_worker_claim(worker_client, prepared, 'pre-submit')
        except Exception as error:
            return {'submitted': False, 'settled': True,
                    'reason': f'original worker lease needs owner reconciliation ({type(error).__name__})'}
    elif token.get('place') == 'validating':
        if (token.get('source_head') != result['head_sha']
                or token.get('target_base') != intent['target_base']):
            return {'submitted': False, 'settled': True,
                    'reason': 'validating task source differs from result intent'}
        if saved_submit is None:
            return {'submitted': False, 'settled': True,
                    'reason': 'validating task has no durable matching submit intent'}
        accepted = _history_has_submit(owner, prepared.plan.project_id,
                                       prepared.task_id, expected_receipt)
        if not accepted:
            return {'submitted': False, 'settled': True,
                    'reason': 'submit outcome is not confirmed in task history; preserve idempotency intent'}
    else:
        return {'submitted': False, 'settled': True,
                'reason': 'task is no longer at its original submit boundary; preserve result for owner reconciliation'}
    # On retry, this GET/history read precedes replay of manual_cord's durable
    # .submit and deterministic Cord message key. It never creates a new key.
    owner.task_workflow(prepared.plan.project_id, prepared.task_id)
    if saved_submit is not None:
        _history_has_submit(owner, prepared.plan.project_id, prepared.task_id, expected_receipt)
    from .manual_cord import send_result
    sent = send_result(owner, prepared.plan.project_id, prepared.plan.checkout,
                       prepared.plan.assignment_dir / 'source', worker=prepared.plan.worker_id,
                       assignment=assignment, result=result, workflow_state=workflow_path,
                       relay_worker=prepared.plan.worker_id)
    receipt = {'schema': 'skybuild.cpu-submitted-result.v1',
               'assignment_id': prepared.assignment_id, 'task_id': prepared.task_id,
               'attempt_id': prepared.attempt_id, 'claim_fence': prepared.claim_fence,
               'head_sha': result['head_sha'], 'message_id': sent.get('message_id'),
               'message_idempotency_key': intent['message_idempotency_key'],
               'result_intent_sha256': _digest(json.dumps(intent, sort_keys=True,
                                                          separators=(',', ':')).encode()),
               'sender_principal': owner.whoami().get('principal_id'),
               'relay_worker': prepared.plan.worker_id, 'sent': sent.get('sent') is True}
    receipt_path = prepared.plan.assignment_dir / 'submitted.json'
    if receipt_path.exists() or receipt_path.is_symlink():
        saved = json.loads(_file_bytes(receipt_path, limit=8192, private=True))
        if saved != receipt:
            raise CPUWorkerBridgeError('Saved submission receipt differs from owner relay result')
    else:
        _write_exclusive(receipt_path, (json.dumps(receipt, sort_keys=True) + '\n').encode())
    return {'submitted': receipt['sent'], 'settled': True, 'state': 'settled',
            'message_id': receipt['message_id']}


def launch_worker(prepared: PreparedCPUWorker) -> dict:
    if prepared.spec is None:
        raise CPUWorkerBridgeError('Reconstructed dispatch handles are reconcile-only')
    with _trusted_client(prepared.plan, prepared.ca_digest,
                         prepared.owner_token_digest) as client:
        return _launch_worker(client, prepared)


def _launch_worker(client: Client, prepared: PreparedCPUWorker) -> dict:
    def verify_pins() -> None:
        if prepared.plan.checkout.resolve(strict=True) != Path(__file__).resolve().parents[2]:
            raise CPUWorkerBridgeError('Worker import checkout differs from the controller source root')
        if _source_head(prepared.plan.checkout) != prepared.source_head:
            raise CPUWorkerBridgeError('Trusted source checkout changed after preparation')
        interpreter = Path(prepared.spec.argv[0]).resolve(strict=True)
        if _digest(_file_bytes(interpreter, limit=256 * 1024 * 1024,
                               private=False, owner=False)) != prepared.interpreter_digest:
            raise CPUWorkerBridgeError('Trusted interpreter changed after preparation')
        if _controller_pin(prepared.plan, prepared.interpreter_digest) != (
                prepared.controller_head, prepared.controller_source_digest,
                prepared.controller_profile_digest):
            raise CPUWorkerBridgeError('Trusted controller or owner profile changed after preparation')

    verify_pins()
    assignment, preclaim, _, assignment_digest = _read_assignment(prepared.plan)
    if (assignment_digest != prepared.assignment_digest
            or assignment.get('assignment_id') != prepared.assignment_id
            or preclaim.get('attempt_id') != prepared.attempt_id
            or preclaim.get('claim_fence') != prepared.claim_fence
            or _digest(_file_bytes(prepared.plan.patch_file, limit=65536, private=True)) != prepared.plan.patch_digest
            or _digest(_file_bytes(prepared.plan.permit_file, limit=16384, private=True)) != prepared.plan.permit_digest):
        raise CPUWorkerBridgeError('Immutable worker snapshots changed after preparation')
    _validate_permit(prepared.plan, assignment, assignment_digest)
    _private_dir(prepared.plan.external_state_dir)
    manager = _unit_manager(prepared.plan.external_state_dir)
    authorization = client.begin_cpu_worker_dispatch(prepared.plan.project_id, prepared.operation_id)
    if authorization.get('start_once') is not True:
        return {'started': False, 'reason': 'one-shot launch authorization was already consumed'}
    verify_pins()
    unit = manager.start(prepared.spec)
    if unit != prepared.unit_name:
        raise CPUWorkerBridgeError('JobUnitManager returned a different unit identity')
    state = manager.observe(unit)
    if (state.launch_nonce != prepared.launch_nonce or not state.invocation_id
            or not re.fullmatch(r'[0-9a-f]{32}', state.invocation_id)):
        raise CPUWorkerBridgeError('Started unit has no exact invocation identity; preserve exposure')
    verify_pins()
    pinned = client.record_cpu_worker_invocation(prepared.plan.project_id, prepared.operation_id,
               {'host_id': socket.gethostname(), 'unit_name': unit,
                'launch_nonce': prepared.launch_nonce, 'invocation_id': state.invocation_id})
    if pinned.get('invocation_id') != state.invocation_id:
        raise CPUWorkerBridgeError('API did not pin the exact systemd invocation')
    return {'started': True, 'unit_name': unit, 'invocation_id': state.invocation_id,
            'launch_nonce': prepared.launch_nonce}


def reconcile_worker(prepared: PreparedCPUWorker) -> dict:
    with _trusted_client(prepared.plan, prepared.ca_digest,
                         prepared.owner_token_digest) as client:
        return _reconcile_worker(client, prepared)


def _reconcile_worker(client: Client, prepared: PreparedCPUWorker) -> dict:
    """Settle exact natural completion, then relay the same immutable result."""
    def verify_pins() -> None:
        if _controller_pin(prepared.plan, prepared.interpreter_digest) != (
                prepared.controller_head, prepared.controller_source_digest,
                prepared.controller_profile_digest):
            raise CPUWorkerBridgeError('Trusted controller or owner profile changed after preparation')

    _private_dir(prepared.plan.external_state_dir)
    manager = _unit_manager(prepared.plan.external_state_dir)
    current = client.get_cpu_worker_dispatch(prepared.plan.project_id, prepared.operation_id)
    latest = current.get('latest_observation')
    if isinstance(latest, dict) and latest.get('phase') == 'failed':
        return {'observed': True, 'settled': False, 'state': current.get('state'),
                'reason': 'failed terminal unit remains held for owner reconciliation'}
    if current.get('state') in {'terminal', 'settled'}:
        state = manager.observe(prepared.unit_name)
    else:
        state = manager.observe(prepared.unit_name)
        if (state.launch_nonce != prepared.launch_nonce or not state.invocation_id):
            return {'observed': False, 'settled': False, 'reason': 'unit identity remains unknown'}
        verify_pins()
        client.record_cpu_worker_invocation(prepared.plan.project_id, prepared.operation_id,
            {'host_id': socket.gethostname(), 'unit_name': state.unit,
             'launch_nonce': state.launch_nonce, 'invocation_id': state.invocation_id})
    if (state.unit != prepared.unit_name or state.launch_nonce != prepared.launch_nonce
            or not state.invocation_id):
        return {'observed': False, 'settled': False, 'reason': 'exact unit invocation remains unknown'}
    if state.phase != 'completed' or state.result != 'success' or state.exit_status != 0:
        if current.get('state') in {'terminal', 'settled'}:
            return {'observed': False, 'settled': current.get('state') == 'settled',
                    'reason': 'persisted success conflicts with current exact invocation; preserve exposure'}
        record = {'observation_id': str(uuid4()), 'host_id': socket.gethostname(),
                  'unit_name': state.unit, 'launch_nonce': state.launch_nonce,
                  'invocation_id': state.invocation_id,
                  'phase': 'running' if state.phase == 'running' else 'unknown',
                  'result': None, 'exit_status': None, 'worker_result_digest': None}
        if state.phase == 'completed':
            record.update(phase='failed', result=state.result or 'unknown-terminal-result',
                          exit_status=state.exit_status)
        observed = client.observe_cpu_worker_dispatch(prepared.plan.project_id,
                                                       prepared.operation_id, record)
        return {'observed': True, 'settled': False, 'state': observed.get('state')}

    assignment, preclaim, workflow, digest = _read_assignment(prepared.plan, allow_runtime=True)
    if digest != prepared.assignment_digest:
        raise CPUWorkerBridgeError('Saved assignment inputs changed after dispatch preparation')
    intent, raw_intent = _result_intent(prepared, assignment, preclaim, workflow)
    _verify_pushed_result(prepared, intent)
    result_digest = _digest(raw_intent)
    if current.get('state') in {'terminal', 'settled'}:
        if (not isinstance(latest, dict) or latest.get('phase') != 'completed'
                or latest.get('result') != 'success' or latest.get('exit_status') != 0
                or latest.get('worker_result_digest') != result_digest
                or latest.get('host_id') != socket.gethostname()
                or latest.get('unit_name') != prepared.unit_name
                or latest.get('launch_nonce') != prepared.launch_nonce
                or latest.get('invocation_id') != state.invocation_id):
            return {'observed': False, 'settled': current.get('state') == 'settled',
                    'reason': 'persisted terminal proof or result digest differs; preserve exposure'}
        observation_id = latest.get('observation_id')
    else:
        observation_id = str(uuid4())
        record = {'observation_id': observation_id, 'host_id': socket.gethostname(),
                  'unit_name': state.unit, 'launch_nonce': prepared.launch_nonce,
                  'invocation_id': state.invocation_id, 'phase': 'completed',
                  'result': 'success', 'exit_status': 0,
                  'worker_result_digest': result_digest}
        intent_path = prepared.plan.assignment_dir / 'observation-intent.json'
        if intent_path.exists() or intent_path.is_symlink():
            try:
                saved = json.loads(_file_bytes(intent_path, limit=4096, private=True))
            except (CPUWorkerBridgeError, ValueError, UnicodeError):
                raise CPUWorkerBridgeError('Saved observation intent is invalid; preserve dispatch') from None
            record = saved.get('request') if isinstance(saved, dict) else None
            if (saved.get('operation_id') != prepared.operation_id or not isinstance(record, dict)
                    or record.get('host_id') != socket.gethostname()
                    or record.get('unit_name') != prepared.unit_name
                    or record.get('launch_nonce') != prepared.launch_nonce
                    or record.get('invocation_id') != state.invocation_id
                    or record.get('worker_result_digest') != result_digest):
                raise CPUWorkerBridgeError('Saved observation intent binds another result')
            observation_id = record['observation_id']
        else:
            _write_exclusive(intent_path, (json.dumps({'operation_id': prepared.operation_id,
                                                       'request': record}, sort_keys=True) + '\n').encode())
        verify_pins()
        observed = client.observe_cpu_worker_dispatch(prepared.plan.project_id,
                                                       prepared.operation_id, record)
        archived = prepared.plan.assignment_dir / ('observation-' + record['observation_id'] + '.json')
        os.replace(intent_path, archived)
        directory = os.open(prepared.plan.assignment_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        if observed.get('state') != 'terminal':
            return {'observed': True, 'settled': False, 'state': observed.get('state')}
    if current.get('state') != 'settled':
        try:
            with _trusted_worker_client(prepared) as worker_client:
                _renew_worker_claim(worker_client, prepared, 'pre-settle')
        except Exception as error:
            return {'observed': True, 'settled': False, 'state': current.get('state'),
                    'reason': f'original worker lease needs owner reconciliation ({type(error).__name__})'}
        verify_pins()
        settled = client.settle_cpu_worker_dispatch(prepared.plan.project_id,
                                                    prepared.operation_id,
                                                    observation_id=observation_id)
        if settled.get('state') != 'settled':
            return {'observed': True, 'settled': False, 'state': settled.get('state')}
    with _trusted_worker_client(prepared) as worker_client:
        submission = _submit_after_settlement(client, worker_client, prepared, intent)
    return {'observed': True, **submission}
