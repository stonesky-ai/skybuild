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
from typing import Any
from uuid import uuid4

from scripts.skybuild_job_unit import JobSpec, JobUnitError, JobUnitManager, JobUnitState

from .contracts import valid_identifier
from . import cpu_worker_dispatch as _dispatch_module
from . import client as _client_module


PROFILE = 'bounded-trusted-cpu-patch-v1'
WORKER_SOURCE = {
    'src/skybuild/__init__.py': '6dc62b0e7d135b949d66c97b99c12155a4ffa60d822b994b9a5d109b1ebe9af1',
    'src/skybuild/client.py': '41d465c78aa2507fc1dcf190be3638ab86725bc6183ee8a014acbd02e2c4db1d',
    'src/skybuild/fleet_preflight.py': 'f2ec5d39b6b1bc0c0a71354a7be89837bd153b5812a9be55f8a951a15fc9424c',
    'src/skybuild/auto_patch_worker.py': 'b25b365ecd4da328fe8be7ef8618ce4a73eb68cd87c50cddf8dd41b4043726fc',
    'src/skybuild/auto_patch_permit.py': '5e7c5de5f3d29cd6ed1aa55d61d9ec1b2a6530163f71f06a68f99396294f7ad1',
    'src/skybuild/manual_assignment.py': '349dc9f367ba63e0bf6c2f3e2d63b7d45c6e5dadcb715f6b67295ad86b65daf7',
    'src/skybuild/manual_cord.py': 'fff868379c3a709019a6212b6c0fcd03078ca5855f265475c2166423857583aa',
    'src/skybuild/manual_dispatch.py': '7baad50262ad315c4d1d48cf6edb27c592b5369b23c3fb81004994a966a667cc',
}
SOURCE_DIGEST = hashlib.sha256(json.dumps(WORKER_SOURCE, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
_HEX64 = re.compile(r'[0-9a-f]{64}\Z')
_SAFE_ENV = ('PATH', 'LANG', 'LC_ALL', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS')
_PRIVATE_ASSIGNMENT_FILES = ('assignment.json', 'assignment.json.workflow.json.intent',
                             'assignment.json.workflow.json', 'preclaim.json')
_CONTROLLER_FILES = {
    'src/skybuild/__init__.py': sys.modules['skybuild'].__file__,
    'src/skybuild/client.py': _client_module.__file__,
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
    weekly_usage_file: Path
    hostwatch_file: Path
    external_state_dir: Path
    controller_profile_file: Path


@dataclass(frozen=True)
class PreparedCPUWorker:
    plan: CPUWorkerPlan
    spec: JobSpec
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
            'schema', 'profile_id', 'controller_head', 'controller_files', 'interpreter_sha256'}:
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


def _read_assignment(plan: CPUWorkerPlan) -> tuple[dict, dict, dict, str]:
    _private_dir(plan.assignment_dir)
    if plan.assignment_dir.resolve().is_relative_to(plan.checkout.resolve()):
        raise CPUWorkerBridgeError('Worker state directory must be outside the source checkout')
    if {item.name for item in plan.assignment_dir.iterdir()} != set(_PRIVATE_ASSIGNMENT_FILES):
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


def _validate_permit(plan: CPUWorkerPlan, assignment: dict, assignment_digest: str) -> tuple[dict, datetime]:
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
            or approved_until.tzinfo is None or approved_until <= datetime.now(timezone.utc)):
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
    try:
        from .auto_patch_permit import check_weekly_usage, resource_admission
        check_weekly_usage(plan.weekly_usage_file, permit)
        resource_admission(plan.hostwatch_file, permit, selected_count=2)
    except (ImportError, ValueError, OSError) as error:
        raise CPUWorkerBridgeError(f'Host or weekly CPU admission failed: {type(error).__name__}') from None
    return permit, approved_until


def prepare_worker(client: Any, plan: CPUWorkerPlan, *, action_id: str, operation_id: str) -> PreparedCPUWorker:
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
    source_head = _source_head(checkout)
    assignment, preclaim, _, assignment_digest = _read_assignment(plan)
    patch = _file_bytes(plan.patch_file, limit=65536, private=True)
    if _digest(patch) != plan.patch_digest:
        raise CPUWorkerBridgeError('Approved patch bytes differ from pinned digest')
    for path in (plan.worker_token_file, plan.git_token_file):
        _file_bytes(path, limit=1024, private=True)
    ca_bytes = _file_bytes(plan.ca_file, limit=1_048_576, private=False, owner=False)
    permit, approved_until = _validate_permit(plan, assignment, assignment_digest)
    interpreter = Path(sys.executable).resolve(strict=True)
    interpreter_info = interpreter.stat()
    if not stat.S_ISREG(interpreter_info.st_mode) or not os.access(interpreter, os.X_OK):
        raise CPUWorkerBridgeError('Trusted Python interpreter is unavailable')
    interpreter_digest = _digest(_file_bytes(interpreter, limit=256 * 1024 * 1024,
                                             private=False, owner=False))
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
    stdin_path = plan.external_state_dir / 'stdin.empty'
    if not stdin_path.exists():
        _write_exclusive(stdin_path, b'')
    _file_bytes(stdin_path, limit=1, private=True, allow_empty=True)
    log_path = plan.external_state_dir / 'worker.log'
    if not log_path.exists():
        _write_exclusive(log_path, b'')
    if log_path.is_symlink() or not log_path.is_file() or log_path.stat().st_uid != os.geteuid() or log_path.stat().st_mode & 0o077:
        raise CPUWorkerBridgeError('Worker log must be a private regular file')
    worker_id = plan.worker_id
    if not isinstance(preclaim.get('message_id'), str) or not preclaim['message_id']:
        raise CPUWorkerBridgeError('Fenced preclaim has no message identity')
    args = (str(interpreter), '-m', 'skybuild.auto_patch_worker', '--url', plan.url,
            '--project', plan.project_id, '--worker', worker_id, '--dispatcher', plan.dispatcher_id,
            '--message-id', preclaim.get('message_id'), '--checkout', str(checkout),
            '--token-file', str(plan.worker_token_file), '--git-token-file', str(plan.git_token_file),
            '--ca-file', str(plan.ca_file), '--patch', str(plan.patch_file),
            '--patch-sha256', plan.patch_digest, '--state-dir', str(worker_state),
            '--approved-until', permit['approved_until'], '--permit', str(plan.permit_file),
            '--permit-sha256', plan.permit_digest)
    argv_digest = _digest(json.dumps({'argv': args, 'interpreter_sha256': interpreter_digest,
                                      'ca_sha256': _digest(ca_bytes), 'source_head': source_head},
                                     sort_keys=True, separators=(',', ':')).encode())
    task_id = assignment.get('task_id')
    attempt_id = preclaim['attempt_id']
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
                             controller_profile_digest,
                             assignment_digest, argv_digest, unit_name, launch_nonce)


def launch_worker(client: Any, manager: JobUnitManager, prepared: PreparedCPUWorker) -> dict:
    def verify_pins() -> None:
        if _source_head(prepared.plan.checkout) != prepared.source_head:
            raise CPUWorkerBridgeError('Trusted source checkout changed after preparation')
        interpreter = Path(prepared.spec.argv[0])
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
    if not manager.state_dir.is_absolute() or manager.state_dir.resolve().is_relative_to(prepared.plan.checkout.resolve()):
        raise CPUWorkerBridgeError('JobUnitManager state must remain outside the source checkout')
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


def reconcile_worker(client: Any, manager: JobUnitManager, prepared: PreparedCPUWorker) -> dict:
    """Persist exact natural completion; any mismatch or uncertainty stays held."""
    def verify_pins() -> None:
        if _controller_pin(prepared.plan, prepared.interpreter_digest) != (
                prepared.controller_head, prepared.controller_source_digest,
                prepared.controller_profile_digest):
            raise CPUWorkerBridgeError('Trusted controller or owner profile changed after preparation')

    current = client.get_cpu_worker_dispatch(prepared.plan.project_id, prepared.operation_id)
    if current.get('state') == 'settled':
        return {'observed': True, 'settled': True, 'state': 'settled'}
    latest = current.get('latest_observation')
    if isinstance(latest, dict) and latest.get('phase') == 'failed':
        return {'observed': True, 'settled': False, 'state': current.get('state'),
                'reason': 'failed terminal unit remains held for owner reconciliation'}
    state = manager.observe(prepared.unit_name)
    if (state.launch_nonce != prepared.launch_nonce or not state.invocation_id):
        return {'observed': False, 'settled': False, 'reason': 'unit identity remains unknown'}
    verify_pins()
    client.record_cpu_worker_invocation(prepared.plan.project_id, prepared.operation_id,
        {'host_id': socket.gethostname(), 'unit_name': state.unit, 'launch_nonce': state.launch_nonce,
         'invocation_id': state.invocation_id})
    phase = 'running' if state.phase == 'running' else 'unknown'
    result = exit_status = worker_result_digest = None
    if state.phase == 'completed':
        path = prepared.plan.assignment_dir / 'submitted.json'
        try:
            raw = _file_bytes(path, limit=32768, private=True)
            submitted = json.loads(raw)
        except (CPUWorkerBridgeError, ValueError, UnicodeError):
            submitted = None
            raw = b''
        if (isinstance(submitted, dict) and submitted.get('sent') is True
                and submitted.get('assignment_id') == prepared.assignment_id
                and isinstance(submitted.get('head_sha'), str)
                and re.fullmatch(r'[0-9a-f]{40,64}', submitted['head_sha'])
                and isinstance(submitted.get('message_id'), str) and submitted['message_id']):
            phase, result, exit_status = 'completed', 'success', 0
            worker_result_digest = _digest(raw)
        else:
            phase, result, exit_status = 'failed', 'terminal-without-verified-worker-result', state.exit_status
        if state.result != 'success' or state.exit_status != 0:
            phase, result, exit_status, worker_result_digest = (
                'failed', state.result or 'unknown-terminal-result', state.exit_status, None)
    observation_id = str(uuid4())
    record = {'observation_id': observation_id, 'host_id': socket.gethostname(),
              'unit_name': state.unit, 'launch_nonce': prepared.launch_nonce,
              'invocation_id': state.invocation_id, 'phase': phase,
              'result': result, 'exit_status': exit_status,
              'worker_result_digest': worker_result_digest}
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
                or record.get('invocation_id') != state.invocation_id):
            raise CPUWorkerBridgeError('Saved observation intent binds another invocation')
        phase = record.get('phase')
    else:
        _write_exclusive(intent_path, (json.dumps({'operation_id': prepared.operation_id,
                                                   'request': record}, sort_keys=True) + '\n').encode())
    verify_pins()
    observed = client.observe_cpu_worker_dispatch(prepared.plan.project_id, prepared.operation_id, record)
    archived = prepared.plan.assignment_dir / ('observation-' + record['observation_id'] + '.json')
    os.replace(intent_path, archived)
    directory = os.open(prepared.plan.assignment_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    if phase != 'completed':
        return {'observed': True, 'settled': False, 'state': observed.get('state')}
    verify_pins()
    settled = client.settle_cpu_worker_dispatch(prepared.plan.project_id, prepared.operation_id,
                                                observation_id=record['observation_id'])
    return {'observed': True, 'settled': settled.get('state') == 'settled', 'state': settled.get('state')}
