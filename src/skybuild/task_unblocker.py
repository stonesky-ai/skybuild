"""CPU-only task diagnosis and guarded release of elapsed UTC deferrals."""
from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import time
from uuid import uuid4

from .client import ClientError
from .completion import current_completion


class UnblockerError(ValueError):
    pass


def stamp(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result if result.tzinfo is not None else None
    except (TypeError, ValueError, AttributeError):
        return None


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def private_dir(path):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink():
        raise UnblockerError('Require an absolute private state path')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise UnblockerError('State directory must be private and owned by this user')
    return path


def read_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise UnblockerError('Require a private owned regular state file')
        raw = stream.read(8 * 1024**2 + 1)
    if len(raw) > 8 * 1024**2:
        raise UnblockerError('State file exceeds its bound')
    return json.loads(raw)


def write_json(path, value, *, create=False):
    private_dir(path.parent)
    if path.exists():
        prior = read_json(path)
        if create:
            if prior != value:
                raise UnblockerError('Durable record already has different content')
            return
    raw = (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()
    if len(raw) > 8 * 1024**2:
        raise UnblockerError('State exceeds its bound')
    temporary = path.with_name('.' + path.name + '.' + uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        if create:
            os.link(temporary, path, follow_symlinks=False)
        else:
            os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def inventory(client, project, *, page_size=100, max_pages=20, check=lambda: None):
    """Finish stable ID pagination before any action. Reject duplicate or moving cursors."""
    seen, result, cursor = set(), [], None
    for _ in range(max_pages):
        check()
        rows = client.list_tasks(project, limit=page_size, after_task_id=cursor, by_id=True)
        if not isinstance(rows, list) or len(rows) > page_size:
            raise UnblockerError('Invalid inventory page')
        ids = [row.get('task_id') for row in rows if isinstance(row, dict)]
        if (len(ids) != len(rows) or any(not isinstance(x, str) or not x for x in ids)
                or ids != sorted(ids) or any(x in seen for x in ids)
                or cursor is not None and any(x <= cursor for x in ids)):
            raise UnblockerError('Inventory IDs are duplicate or not strictly increasing')
        seen.update(ids); result.extend(rows)
        if len(rows) < page_size:
            return result
        cursor = ids[-1]
    raise UnblockerError('Inventory page bound reached; no actions permitted')


def diagnose(task, workflow, execution, all_tasks, now):
    """Return deterministic facts and guidance. No fact grants worker admission."""
    if task.get('status') in {'done', 'superseded'}:
        return {'codes': ['immutable'], 'guidance': 'Keep the accepted or retired task unchanged', 'action': None}
    petri = task.get('metadata', {}).get('_skybuild_workflow', {}).get('petri', {})
    if petri.get('schema_version') != 1 or not isinstance(workflow, dict) or 'token' not in workflow:
        codes = ['legacy_or_unverified_workflow', 'no_api_journal_route']
        notes = ['Owner must verify and enroll the task through the accepted workflow']
        dependencies = [all_tasks.get(x) for x in task.get('dependencies', [])]
        if dependencies:
            complete = all(d and current_completion(d) for d in dependencies)
            codes.append('dependencies_current' if complete else 'dependencies_unresolved')
            notes.append('Reassess the stated dependency blocker; do not infer Ready from task status')
        if 'memory' in str(task.get('blocker') or '').lower():
            codes.append('host_admission_scope')
            notes.append('Host memory belongs to worker admission')
        return {'codes': codes, 'guidance': '; '.join(notes), 'action': None}
    token = workflow['token']
    codes, notes = [], []
    pending = token.get('pending_action')
    if pending:
        codes.append('pending_action'); notes.append('Reconcile the existing pending action; do not repeat its external effect')
    truncated = any(execution.get(s, {}).get('truncated', True) for s in ('effects', 'reservations', 'observations'))
    effects = execution.get('effects', {}).get('items', [])
    reservations = execution.get('reservations', {}).get('items', [])
    held = any(x.get('exposure_held') for x in effects) or any(x.get('state') == 'reserved' for x in reservations)
    claim = execution.get('claim') or {}
    if truncated:
        codes.append('execution_evidence_truncated'); notes.append('Obtain complete execution evidence; absence is not proved')
    if held:
        codes.append('held_exposure'); notes.append('Retain the effect and CPU hold until exact stopped or terminal evidence is accepted')
    if claim.get('held'):
        until = stamp(claim.get('lease_until'))
        codes.append('expired_claim' if until and until <= now else 'held_claim')
        notes.append('Reconcile the exact claim and worker through the accepted fenced route; expiry does not clear exposure')
    place = token.get('place')
    if place == 'working' and not claim.get('held'):
        codes.append('missing_claim'); notes.append('Verify the current attempt and claim binding before any worker action')
    if place == 'working' and not any(x.get('attempt_id') == token.get('attempt_id') for x in reservations):
        codes.append('missing_recorded_worker_binding'); notes.append('No CPU reservation for this attempt is recorded; verify the accepted worker binding')
    if place in {'validating', 'integrating'}:
        fields = ('source_head', 'target_base', 'definition_revision', 'input_generation', 'policy_version', 'attempt_id', 'claim_fence')
        passed = {r.get('stage') for r in token.get('evidence', [])
                  if r.get('state') in {'passed', 'not_applicable'} and all(r.get(k) == token.get(k) for k in fields)}
        missing = sorted(set(token.get('requirements', [])) - passed)
        if missing:
            codes.append('current_validation_missing'); notes.append('Obtain current authorized evidence for: ' + ', '.join(missing))
        if place == 'integrating':
            codes.append('integration_acceptance_pending'); notes.append('Verify the exact bundle gate, publication and task acceptance; never fabricate completion')
    dependencies = [all_tasks.get(x) for x in task.get('dependencies', [])]
    if dependencies:
        complete = all(d and current_completion(d) and
                       d.get('metadata', {}).get('_skybuild_workflow', {}).get('readiness', {}).get('input_generation') ==
                       d.get('metadata', {}).get('_skybuild_workflow', {}).get('readiness', {}).get('assessed_generation')
                       for d in dependencies)
        codes.append('dependencies_current' if complete else 'dependencies_unresolved')
        notes.append('Current completion is visible; owner must recheck the stated blocker' if complete else
                     'Keep dependency guards; obtain current accepted completion of each prerequisite')
    if 'memory' in str(task.get('blocker') or '').lower():
        codes.append('host_admission_scope'); notes.append('Host memory belongs to worker admission; do not infer permanent task ineligibility')
    action = None
    until = stamp(token.get('deferred_until'))
    if (place == 'deferred' and until and until <= now and not token.get('milestone_task_id')
            and not pending and not truncated and not held and not claim.get('held')
            and 'resume_deferred' in workflow.get('available_actions', [])):
        codes.append('elapsed_utc_deferral'); notes.append('The explicit UTC deferral has elapsed; resume only to Ready for reassessment')
        action = 'resume_deferred'
    safe_control = not pending and not truncated and not held and not claim.get('held')
    original_blocker = original_text(task.get('blocker') or token.get('hold_reason') or '')
    future_utc = place == 'deferred' and until and until > now and not token.get('milestone_task_id')
    dependency_blocker = ('dependencies_unresolved' in codes and
                          ('dependenc' in original_blocker.lower() or any(
                              name in original_blocker for name in task.get('dependencies', []))))
    if (not action and place in {'hold', 'deferred'} and safe_control and original_blocker
            and (future_utc or dependency_blocker) and 'update_control' in workflow.get('available_actions', [])):
        codes.append('verified_blocker_retained')
        notes.append('Attach current facts to the existing blocker; keep this place and every admission guard')
        action = 'update_control'
    if not action:
        codes.append('no_api_journal_route')
        notes.append('No supported automatic API journal route is proved for this finding')
    if not codes:
        codes.append('no_clearance_proof'); notes.append('Retain the current state until its stated blocker has exact proof')
    return {'codes': codes, 'guidance': '; '.join(notes), 'action': action}


def history_proves(client, project, task_id, operation, revision, *, event='resume_deferred',
                   place='deferred', check=lambda: None):
    for offset in range(0, 2000, 100):
        check()
        rows = client.task_history(project, task_id, limit=100, offset=offset)
        for row in rows:
            facts = row.get('event_facts') or {}
            if (facts.get('operation_id') == operation and row.get('operation') == 'workflow.' + event
                    and row.get('revision') == revision + 1
                    and facts.get('project_id') == project and facts.get('task_id') == task_id
                    and facts.get('event') == event and facts.get('from_place') == place
                    and facts.get('to_place') == ('ready' if event == 'resume_deferred' else place)):
                return True
        if len(rows) < 100:
            return False
    return False


NOTE = '\n[TaskUnblocker verified at '


def original_text(value):
    return value.split(NOTE, 1)[0] if isinstance(value, str) else value


def stable_evidence(value):
    """Remove this tool's appended verification note and its own revision changes."""
    if isinstance(value, list):
        return [stable_evidence(x) for x in value]
    if isinstance(value, dict):
        return {k: stable_evidence(v) for k, v in value.items()}
    return original_text(value)


def finding_fingerprint(evidence, decision, binding):
    value = stable_evidence({'evidence': evidence, 'decision': decision, 'source_binding': binding})
    value['evidence']['task'].pop('revision', None)
    token = value['evidence']['workflow'].get('token') or {}
    token.pop('revision', None)
    petri = value['evidence']['task']['metadata'].get('_skybuild_workflow') or {}
    petri.get('petri', {}).get('token', {}).pop('revision', None)
    value['source_binding'].pop('task_revision', None)
    return fingerprint(value)



def task_facts(task):
    """Select definition and workflow fields; omit display and heartbeat fields."""
    facts = {k: task.get(k) for k in ('task_id', 'revision', 'status', 'phase', 'blocker', 'dependencies')}
    metadata = task.get('metadata', {})
    facts['metadata'] = {k: metadata.get(k) for k in ('_skybuild_workflow', '_skybuild_completion')}
    return facts


def decision_evidence(task, workflow, execution, by_id):
    sections = {}
    for name in ('effects', 'reservations', 'observations'):
        section = execution.get(name, {})
        sections[name] = {'truncated': section.get('truncated', True), 'items': [
            {k: v for k, v in row.items() if k not in {'updated_at', 'observed_at', 'received_at', 'heartbeat_at', 'created_at'}}
            for row in section.get('items', [])]}
    claim = execution.get('claim') or {}
    sections['claim'] = {k: claim.get(k) for k in ('held', 'lease_until', 'holder', 'fence', 'claim_revision', 'task_revision')}
    return {'task': task_facts(task), 'workflow': {'token': (workflow or {}).get('token'),
            'available_actions': (workflow or {}).get('available_actions')}, 'execution': sections,
            'dependencies': {x: task_facts(by_id[x]) if x in by_id else None for x in task.get('dependencies', [])}}


def source_binding(task, workflow, execution):
    token = (workflow or {}).get('token', {})
    rows = [r for r in execution.get('reservations', {}).get('items', [])
            if r.get('attempt_id') == token.get('attempt_id')]
    workers = {r.get('worker_id') for r in rows if r.get('worker_id')}
    return {'task_id': task['task_id'], 'task_revision': task['revision'],
            'attempt_id': token.get('attempt_id'), 'claim_fence': token.get('claim_fence'),
            'source_ref': token.get('source_branch'), 'source_commit': token.get('source_head'),
            'target_base': token.get('target_base'), 'worker_id': next(iter(workers)) if len(workers) == 1 else None,
            'repository_url': None, 'repository_id': None, 'host_id': None,
            'assignment_sha256': None, 'proof_sha256': None, 'starting_commit': None,
            'checkout_path': None, 'worktree_path': None,
            'database_gap': 'The accepted task API does not expose a durable repository or worktree binding'}


def run(client, project, state, *, apply=False, expected_principal, api_url, seconds=120, page_size=100, max_pages=20):
    state = private_dir(state)
    lock = os.open(state / 'run.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(lock)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise UnblockerError('Overlap lock must be private and owned')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'schema': 'skybuild.task-unblocker.v1', 'status': 'overlap_skipped'}
        binding = {'schema': 'skybuild.task-unblocker-state.v1', 'project_id': project,
                   'principal_id': expected_principal, 'api_url': api_url}
        write_json(state / 'binding.json', binding, create=True)
        deadline = time.monotonic() + seconds
        def check():
            if time.monotonic() >= deadline:
                raise UnblockerError('Sweep deadline reached')
        identity = client.whoami()
        if identity.get('principal_id') != expected_principal:
            raise UnblockerError('API principal differs from the explicit pin')
        tasks = inventory(client, project, page_size=page_size, max_pages=max_pages, check=check)
        by_id = {t['task_id']: t for t in tasks}
        cache_path = state / ('last-apply.json' if apply else 'last-dry-run.json')
        previous = read_json(cache_path) if cache_path.exists() else {}
        next_cache, findings, actions, bindings = {}, [], [], []
        unresolved = set()
        action_root = state / 'actions'
        if action_root.is_dir():
            journals = sorted(action_root.iterdir())
            if len(journals) > 2000:
                raise UnblockerError('Action journal scan bound reached')
            for journal in journals:
                if journal.is_symlink() or not journal.is_dir():
                    raise UnblockerError('Invalid action journal path')
                if not (journal / 'sent.json').exists() or (journal / 'confirmed.json').exists():
                    continue
                intent = read_json(journal / 'intent.json')
                outcome = read_json(journal / 'outcome.json') if (journal / 'outcome.json').exists() else {}
                if outcome.get('outcome') == 'rejected':
                    continue
                if history_proves(client, project, intent['task_id'], intent['operation_id'], intent['revision'],
                                  event=intent.get('event', 'resume_deferred'), place=intent.get('place', 'deferred'), check=check):
                    write_json(journal / 'confirmed.json', {'history_proven': True}, create=True)
                else:
                    unresolved.add(intent['task_id'])
                    actions.append({'task_id': intent['task_id'], 'outcome': 'ambiguous_no_replay'})
        observed = datetime.now(timezone.utc)
        for task in tasks:
            check()
            if task.get('status') not in {'blocked', 'deferred', 'in-progress'}:
                continue
            task_id = task['task_id']
            workflow = None
            if task.get('metadata', {}).get('_skybuild_workflow', {}).get('petri', {}).get('schema_version') == 1:
                workflow = client.task_workflow(project, task_id)
                if workflow.get('task', {}).get('revision') != task['revision']:
                    continue
            execution = client.execution_status(project, task_id, limit=100)
            if execution.get('revision') != task['revision']:
                continue
            decision = diagnose(task, workflow, execution, by_id, observed)
            evidence = decision_evidence(task, workflow, execution, by_id)
            binding = source_binding(task, workflow, execution)
            operation_ids = sorted({row['operation_id'] for row in execution.get('effects', {}).get('items', [])
                                    if row.get('attempt_id') == binding['attempt_id'] and row.get('operation_id')})
            dispatch_bindings = []
            for operation_id in operation_ids:
                check()
                try:
                    dispatch = client.get_cpu_worker_dispatch(project, operation_id)
                except ClientError:
                    continue  # A generic effect need not be a CPU dispatch. Keep the gap explicit.
                if (dispatch.get('task_id') == task_id and dispatch.get('attempt_id') == binding['attempt_id']
                        and dispatch.get('claim_fence') == binding['claim_fence']):
                    dispatch_bindings.append({k: dispatch.get(k) for k in
                        ('operation_id', 'worker_id', 'host_id', 'assignment_digest', 'source_head')})
            binding['recorded_dispatches'] = dispatch_bindings
            if len(dispatch_bindings) == 1:
                row = dispatch_bindings[0]
                binding.update(worker_id=row['worker_id'], host_id=row['host_id'], assignment_sha256=row['assignment_digest'])
            bindings.append(binding)
            fp = finding_fingerprint(evidence, decision, binding)
            next_cache[task_id] = fp
            if previous.get(task_id) != fp:
                finding = {'task_id': task_id, 'revision': task['revision'], 'fingerprint': fp,
                           'verified_at_utc': observed.isoformat(), **decision,
                           'evidence_sources': {'task_updated_at': task.get('updated_at'),
                               'task_created_at': task.get('created_at'),
                               'task_api_path': '/api/v1/projects/' + project + '/tasks/' + task_id,
                               'workflow_api_path': '/api/v1/projects/' + project + '/tasks/' + task_id + '/workflow',
                               'execution_api_path': '/api/v1/projects/' + project + '/tasks/' + task_id + '/execution-status',
                               'observation_times': [{k: row.get(k) for k in ('event_id','observed_at','received_at')}
                                   for row in execution.get('observations', {}).get('items', [])],
                               'check_provenance': 'Authenticated GETs; deterministic diagnosis; external artifact bytes were not reverified'},
                           'source_binding': binding}
                finding_path = state / 'findings' / (fp + '.json')
                if finding_path.exists():
                    if read_json(finding_path).get('fingerprint') != fp:
                        raise UnblockerError('Retained finding fingerprint changed')
                else:
                    write_json(finding_path, finding, create=True)
                    findings.append(finding)
            if not apply or not decision['action'] or task_id in unresolved:
                continue
            # Revision and all diagnosis inputs are re-read before the one send.
            check()
            fresh = client.get_task(project, task_id)
            fresh_workflow = client.task_workflow(project, task_id)
            fresh_execution = client.execution_status(project, task_id, limit=100)
            fresh_dependencies = dict(by_id)
            for dependency in task.get('dependencies', []):
                check()
                fresh_dependencies[dependency] = client.get_task(project, dependency)
            fresh_decision = diagnose(fresh, fresh_workflow, fresh_execution, fresh_dependencies, datetime.now(timezone.utc))
            if (decision_evidence(fresh, fresh_workflow, fresh_execution, fresh_dependencies) != evidence
                    or fresh_decision['action'] != decision['action']):
                actions.append({'task_id': task_id, 'outcome': 'changed_before_send'}); continue
            operation = 'taskunblocker-' + fp
            journal = private_dir(state / 'actions' / operation)
            intent_path = journal / 'intent.json'
            if intent_path.exists():
                intent = read_json(intent_path)
            else:
                intent = {'project_id': project, 'task_id': task_id, 'revision': task['revision'],
                          'fingerprint': fp, 'operation_id': operation, 'event': decision['action'],
                          'place': workflow['token']['place'],
                          'body': {'reason': 'TaskUnblocker verified elapsed UTC deferral at ' + observed.isoformat() + '; evidence SHA-256 ' + fp,
                                   'next_action': 'Reassess the current task definition and dependencies'}}
                if decision['action'] == 'update_control':
                    until = workflow['token'].get('deferred_until')
                    facts = ('Explicit deferred_until=' + until + ' was future at verification' if until else
                             'Dependencies lacked current accepted completion at verification: ' + ','.join(
                                 name for name in task.get('dependencies', [])
                                 if not by_id.get(name) or not current_completion(by_id[name])))
                    if task.get('updated_at'):
                        facts += '; source task updated_at=' + str(task['updated_at'])
                    note = NOTE + observed.isoformat() + '; evidence SHA-256 ' + fp + '; facts: ' + facts + ']'
                    reason = original_text(task.get('blocker') or workflow['token'].get('hold_reason') or '')
                    next_action = original_text(task.get('next_action') or workflow['token'].get('next_action') or 'Recheck the retained blocker')
                    intent['body'] = {'reason': reason + note, 'next_action': next_action + note}
                    if any(len(value) > 4096 for value in intent['body'].values()):
                        actions.append({'task_id': task_id, 'outcome': 'control_note_exceeds_bound'})
                        continue
                write_json(intent_path, intent, create=True)
            if (journal / 'sent.json').exists():
                proven = history_proves(client, project, task_id, operation, intent['revision'],
                                        event=intent.get('event', 'resume_deferred'), place=intent.get('place', 'deferred'), check=check)
                actions.append({'task_id': task_id, 'outcome': 'confirmed_history' if proven else 'ambiguous_no_replay'})
                if proven:
                    write_json(journal / 'confirmed.json', {'history_proven': True}, create=True)
                continue
            write_json(journal / 'sent.json', {'operation_id': operation}, create=True)
            try:
                client.workflow_transition(project, task_id, intent.get('event', 'resume_deferred'), intent['body'],
                                           expected_revision=intent['revision'], idempotency_key=operation)
            except ClientError as error:
                outcome = 'rejected' if error.status_code in {400, 401, 403, 404, 409, 422} else 'ambiguous_no_replay'
                write_json(journal / 'outcome.json', {'outcome': outcome, 'code': error.code,
                                                     'http_status': error.status_code}, create=True)
                actions.append({'task_id': task_id, 'outcome': outcome}); continue
            proven = history_proves(client, project, task_id, operation, intent['revision'],
                                        event=intent.get('event', 'resume_deferred'), place=intent.get('place', 'deferred'), check=check)
            actions.append({'task_id': task_id, 'outcome': 'confirmed_history' if proven else 'ambiguous_no_replay'})
            if proven:
                write_json(journal / 'confirmed.json', {'history_proven': True}, create=True)
        write_json(cache_path, next_cache)
        report = {'schema': 'skybuild.task-unblocker.v1', 'verified_at_utc': observed.isoformat(),
                  'status': 'complete', 'inventory_complete': True, 'scanned': len(tasks), 'checked': len(next_cache),
                  'apply': apply, 'findings': findings, 'actions': actions, 'source_bindings': bindings,
                  'models_invoked': 0, 'source_dates_changed': False}
        write_json(state / 'status.json', report)
        return report
    finally:
        os.close(lock)
