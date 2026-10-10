import pytest
from types import SimpleNamespace

import skybuild.cpu_worker_bridge as bridge
from skybuild.cpu_worker_bridge import (CPUWorkerBridgeError, _attempt_log_path,
                                        _controller_pin, _file_bytes)


def test_empty_stdin_is_allowed_only_when_explicit(tmp_path):
    empty = tmp_path / "stdin.empty"
    empty.write_bytes(b"")
    empty.chmod(0o600)

    with pytest.raises(CPUWorkerBridgeError, match="bounded private regular file"):
        _file_bytes(empty, limit=1, private=True)

    assert _file_bytes(empty, limit=1, private=True, allow_empty=True) == b""


def test_controller_pin_rejects_a_different_worker_import_checkout(tmp_path):
    checkout = tmp_path / "different-checkout"
    checkout.mkdir()
    plan = SimpleNamespace(checkout=checkout)

    with pytest.raises(CPUWorkerBridgeError, match="exact controller source root"):
        _controller_pin(plan, "a" * 64)


def test_worker_log_paths_are_attempt_scoped(tmp_path):
    assert _attempt_log_path(tmp_path, 'attempt-1') != _attempt_log_path(tmp_path, 'attempt-2')


def test_result_intent_binds_original_claim_and_exact_cord_body(tmp_path):
    assignment = {'assignment_id': 'assignment-1', 'task_id': 'SKYBUILD-TASK-TEST',
                  'worker': 'worker-1', 'dispatcher': 'owner-1', 'branch': 'task/test',
                  'base_sha': 'b' * 40, 'brief_sha256': 'c' * 64}
    token = {'project_id': 'skybuild', 'task_id': assignment['task_id'],
             'attempt_id': 'attempt-1', 'claim_fence': 7, 'input_generation': 2,
             'definition_revision': 3, 'policy_version': 1}
    result = {'schema': 'manual-work-v1', 'assignment_id': assignment['assignment_id'],
              'phase': 'ready-for-review', 'branch': assignment['branch'],
              'head_sha': 'a' * 40, 'checks': ['focused checks passed'],
              'changed_paths': ['src/skybuild/client.py'], 'risks': [],
              'next_action': 'Independent review'}
    message, key = bridge._manual_cord_module.result_message(assignment, result)
    intent = {'schema': 'skybuild.cpu-result-intent.v1', 'project_id': 'skybuild',
              'task_id': assignment['task_id'], 'assignment_id': assignment['assignment_id'],
              'worker': 'worker-1', 'attempt_id': 'attempt-1', 'claim_fence': 7,
              'input_generation': 2, 'definition_revision': 3, 'policy_version': 1,
              'assignment_sha256': bridge._digest(json.dumps(
                  assignment, sort_keys=True, separators=(',', ':')).encode()),
              'brief_sha256': assignment['brief_sha256'], 'patch_sha256': 'd' * 64,
              'source_head': result['head_sha'], 'source_branch': 'refs/heads/task/test',
              'target_base': assignment['base_sha'], 'result': result,
              'message': message, 'message_idempotency_key': key}
    path = tmp_path / 'result-intent.json'
    path.write_text(json.dumps(intent, sort_keys=True) + '\n')
    path.chmod(0o600)
    prepared = SimpleNamespace(plan=SimpleNamespace(assignment_dir=tmp_path,
                                                    project_id='skybuild', worker_id='worker-1',
                                                    patch_digest='d' * 64),
                               task_id=assignment['task_id'], assignment_id='assignment-1',
                               attempt_id='attempt-1', claim_fence=7)
    got, raw = bridge._result_intent(prepared, assignment,
                                     {'attempt_id': 'attempt-1', 'claim_fence': 7},
                                     {'token': token})
    assert got == intent
    assert bridge._digest(raw) == bridge._digest(path.read_bytes())
    intent['claim_fence'] = 8
    path.write_text(json.dumps(intent, sort_keys=True) + '\n')
    with pytest.raises(CPUWorkerBridgeError, match='exact assignment or worker fence'):
        bridge._result_intent(prepared, assignment,
                              {'attempt_id': 'attempt-1', 'claim_fence': 7}, {'token': token})


def test_terminal_observation_retries_settlement_without_new_observation(tmp_path, monkeypatch):
    operation_id, observation_id = "operation-1", "11111111-1111-4111-8111-111111111111"
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    plan = SimpleNamespace(project_id="project-1", external_state_dir=state_dir)
    prepared = SimpleNamespace(plan=plan, operation_id=operation_id,
                               controller_head="a" * 40, controller_source_digest="b" * 64,
                               controller_profile_digest="c" * 64, interpreter_digest="0" * 64,
                               ca_digest="1" * 64, owner_token_digest="2" * 64,
                               unit_name="skybuild-job-" + "d" * 24 + ".service",
                               launch_nonce="e" * 32)
    latest = {'phase': 'completed', 'result': 'success', 'exit_status': 0,
              'worker_result_digest': 'f' * 64, 'host_id': 'test-host',
              'unit_name': prepared.unit_name, 'launch_nonce': prepared.launch_nonce,
              'invocation_id': '1' * 32, 'observation_id': observation_id}

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get_cpu_worker_dispatch(self, project, operation):
            return {'state': 'settled', 'latest_observation': latest}

        def settle_cpu_worker_dispatch(self, project, operation, *, observation_id):
            assert observation_id == latest['observation_id']
            return {'state': 'settled'}

        def observe_cpu_worker_dispatch(self, *_args):
            pytest.fail('terminal observation replay must not append a new observation')

    class Manager:
        def observe(self, unit):
            return SimpleNamespace(unit=unit, phase='completed', result='success', exit_status=0,
                                   launch_nonce=prepared.launch_nonce, invocation_id=latest['invocation_id'])

    class WorkerClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(bridge.socket, 'gethostname', lambda: 'test-host')
    monkeypatch.setattr(bridge, '_controller_pin',
                        lambda _plan, _digest: (prepared.controller_head,
                                                prepared.controller_source_digest,
                                                prepared.controller_profile_digest))
    monkeypatch.setattr(bridge, '_unit_manager', lambda _path: Manager())
    monkeypatch.setattr(bridge, '_trusted_client', lambda *_args: Client())
    monkeypatch.setattr(bridge, '_trusted_worker_client', lambda _prepared: WorkerClient())
    monkeypatch.setattr(bridge, '_read_assignment', lambda _plan: ({'assignment_id': 'assignment-1'},
                        {'attempt_id': 'attempt-1', 'claim_fence': 1}, {'token': {}}, '0' * 64))
    intent = {'result': {'head_sha': 'a' * 40}}
    monkeypatch.setattr(bridge, '_result_intent', lambda *_args: (intent, b'{}'))
    monkeypatch.setattr(bridge, '_verify_pushed_result', lambda *_args: None)
    monkeypatch.setattr(bridge, '_submit_after_settlement',
                        lambda *_args: {'submitted': True, 'settled': True, 'state': 'settled'})
    result = bridge.reconcile_worker(prepared)
    assert result == {'observed': True, 'submitted': True, 'settled': True, 'state': 'settled'}
