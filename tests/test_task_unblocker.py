from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import json
import os

import pytest

from skybuild.task_unblocker import UnblockerError, diagnose, inventory, run, fingerprint, decision_evidence


class Fake:
    def __init__(self):
        self.task = {'task_id': 'T', 'revision': 1, 'status': 'deferred', 'dependencies': [],
                     'metadata': {'_skybuild_workflow': {'petri': {'schema_version': 1}}}}
        self.token = {'place': 'deferred', 'deferred_until': '2020-01-01T00:00:00Z', 'attempt_id': 'A'}
        self.execution = {'revision': 1, 'claim': None, **{s: {'items': [], 'truncated': False}
                          for s in ('effects', 'reservations', 'observations')}}
        self.posts = []; self.history = []; self.ambiguous = False
    def whoami(self): return {'principal_id': 'owner'}
    def list_tasks(self, *a, **kw): return [deepcopy(self.task)]
    def get_task(self, *a): return deepcopy(self.task)
    def task_workflow(self, *a):
        return {'task': deepcopy(self.task), 'token': deepcopy(self.token), 'available_actions': ['resume_deferred']}
    def execution_status(self, *a, **kw): return deepcopy(self.execution)
    def task_history(self, *a, **kw): return deepcopy(self.history)
    def workflow_transition(self, *a, **kw):
        self.posts.append(kw)
        if self.ambiguous: raise OSError('Transport ended after send')
        self.history = [{'event_facts': {'operation_id': kw['idempotency_key'], 'project_id': 'P', 'task_id': 'T',
                         'event': 'resume_deferred', 'from_place': 'deferred', 'to_place': 'ready'},
                         'operation': 'workflow.resume_deferred', 'revision': 2}]


def invoke(client, state, **kw):
    return run(client, 'P', state, expected_principal='owner', api_url='https://host', **kw)


def test_stable_cursor_requires_final_short_page():
    class Pages:
        def __init__(self): self.cursors = []
        def list_tasks(self, *a, **kw):
            self.cursors.append(kw['after_task_id'])
            return [{'task_id': 'A'}] if kw['after_task_id'] is None else []
    c = Pages()
    assert inventory(c, 'P', page_size=1) == [{'task_id': 'A'}]
    assert c.cursors == [None, 'A']
    with pytest.raises(UnblockerError): inventory(c, 'P', page_size=1, max_pages=1)


def test_duplicate_page_fails_closed():
    class Pages:
        def list_tasks(self, *a, **kw): return [{'task_id': 'A'}, {'task_id': 'A'}]
    with pytest.raises(UnblockerError): inventory(Pages(), 'P', page_size=2)


def test_dry_run_and_unchanged_finding(tmp_path):
    c = Fake(); state = tmp_path / 'state'
    first = invoke(c, state); second = invoke(c, state)
    assert len(first['findings']) == 1 and second['findings'] == [] and c.posts == []
    assert second['source_bindings'][0]['repository_url'] is None
    assert second['source_bindings'][0]['task_revision'] == 1
    assert first['models_invoked'] == 0


def test_elapsed_deferral_send_once_history_confirmed(tmp_path):
    c = Fake(); state = tmp_path / 'state'
    assert invoke(c, state, apply=True)['actions'][0]['outcome'] == 'confirmed_history'
    invoke(c, state, apply=True)
    assert len(c.posts) == 1 and c.posts[0]['expected_revision'] == 1


def test_ambiguous_send_never_replayed_even_after_changed_fingerprint(tmp_path):
    c = Fake(); c.ambiguous = True; state = tmp_path / 'state'
    with pytest.raises(OSError): invoke(c, state, apply=True)
    c.task['blocker'] = 'Changed reason'
    report = invoke(c, state, apply=True)
    assert len(c.posts) == 1 and report['actions'][0]['outcome'] == 'ambiguous_no_replay'


@pytest.mark.parametrize('change', ['effect', 'claim', 'pending', 'truncated', 'milestone'])
def test_exposure_and_other_guards_preserved(change):
    c = Fake()
    if change == 'effect': c.execution['effects']['items'] = [{'exposure_held': True}]
    if change == 'claim': c.execution['claim'] = {'held': True, 'lease_until': '2020-01-01T00:00:00Z'}
    if change == 'pending': c.token['pending_action'] = {'id': 'X'}
    if change == 'truncated': c.execution['observations']['truncated'] = True
    if change == 'milestone': c.token['milestone_task_id'] = 'M'
    assert diagnose(c.task, c.task_workflow(), c.execution, {}, datetime.now(timezone.utc))['action'] is None


def test_overlap_lock_skips_all_reads(tmp_path):
    state = tmp_path / 'state'; state.mkdir(mode=0o700)
    fd = os.open(state / 'run.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert invoke(Fake(), state)['status'] == 'overlap_skipped'
    finally: os.close(fd)


def test_heartbeat_does_not_change_decision_fingerprint():
    c = Fake(); c.execution['observations']['items'] = [{'state': 'terminal', 'observed_at': 'old'}]
    first = decision_evidence(c.task, c.task_workflow(), c.execution, {})
    c.execution['observations']['items'][0]['observed_at'] = 'new'
    assert fingerprint(first) == fingerprint(decision_evidence(c.task, c.task_workflow(), c.execution, {}))


def test_state_identity_cannot_change(tmp_path):
    c = Fake(); invoke(c, tmp_path / 'state')
    with pytest.raises(UnblockerError):
        run(c, 'OTHER', tmp_path / 'state', expected_principal='owner', api_url='https://host')


def test_changed_revision_prevents_send(tmp_path):
    c = Fake()
    def moved(*args):
        result = deepcopy(c.task); result['revision'] = 2; return result
    c.get_task = moved
    report = invoke(c, tmp_path / 'state', apply=True)
    assert report['actions'][0]['outcome'] == 'changed_before_send' and not c.posts


def test_completed_tasks_receive_no_follow_up_reads(tmp_path):
    c = Fake(); c.task['status'] = 'done'
    def forbidden(*args, **kw): raise AssertionError('Follow-up read is forbidden')
    c.task_workflow = c.execution_status = forbidden
    assert invoke(c, tmp_path / 'state')['checked'] == 0


def test_validation_diagnosis_preserves_current_evidence_requirement():
    c = Fake(); c.task['status'] = 'in-progress'
    c.token.update(place='validating', requirements=['unit_tests'], evidence=[])
    result = diagnose(c.task, c.task_workflow(), c.execution, {}, datetime.now(timezone.utc))
    assert 'current_validation_missing' in result['codes'] and result['action'] is None


def test_watch_pins_command_and_apply_is_explicit(monkeypatch):
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / 'scripts/task_unblocker_watch.py'
    spec = importlib.util.spec_from_file_location('task_unblocker_watch', path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    class Result:
        def __init__(self, stdout): self.stdout = stdout
    monkeypatch.setattr(module.socket, 'gethostname', lambda: 'wonko')
    monkeypatch.setattr(module.subprocess, 'run', lambda cmd, **kw: Result('abc\n' if 'rev-parse' in cmd else ''))
    config = dict(checkout=str(module.ROOT), source_head='abc', expected_host='wonko', api_url='https://host',
                  ca_file='/ca', token_file='/private/token', principal='owner', project='P', state='/state', apply=False)
    assert '--apply' not in module.command(config)
    config['apply'] = True
    assert module.command(config)[-1] == '--apply'
    config['source_head'] = 'different'
    with pytest.raises(UnblockerError): module.command(config)


def test_watch_timeout_kills_late_child(tmp_path):
    import importlib.util
    from pathlib import Path
    import sys
    path = Path(__file__).resolve().parents[1] / 'scripts/task_unblocker_watch.py'
    spec = importlib.util.spec_from_file_location('task_unblocker_watch_timeout', path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    saved = tmp_path / 'late-effect'
    code = 'import time,pathlib;time.sleep(1);pathlib.Path(' + repr(str(saved)) + ').write_text("late")'
    assert module.bounded_run([sys.executable, '-c', code], seconds=0.05) == 1
    import time
    time.sleep(1.1)
    assert not saved.exists()


def test_unrelated_metadata_heartbeat_does_not_repeat_finding(tmp_path):
    c = Fake(); c.task['metadata']['telemetry'] = {'heartbeat_at': 'old'}
    first = invoke(c, tmp_path / 'state')
    c.task['metadata']['telemetry']['heartbeat_at'] = 'new'
    second = invoke(c, tmp_path / 'state')
    assert len(first['findings']) == 1 and second['findings'] == []
