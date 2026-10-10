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


def test_terminal_observation_retries_settlement_without_new_observation(monkeypatch):
    operation_id, observation_id = "operation-1", "11111111-1111-4111-8111-111111111111"
    plan = SimpleNamespace(project_id="project-1")
    prepared = SimpleNamespace(plan=plan, operation_id=operation_id,
                               controller_head="a" * 40, controller_source_digest="b" * 64,
                               controller_profile_digest="c" * 64, interpreter_digest="0" * 64,
                               unit_name="skybuild-job-" + "d" * 24 + ".service",
                               launch_nonce="e" * 32)
    latest = {'phase': 'completed', 'result': 'success', 'exit_status': 0,
              'worker_result_digest': 'f' * 64, 'host_id': 'test-host',
              'unit_name': prepared.unit_name, 'launch_nonce': prepared.launch_nonce,
              'invocation_id': '1' * 32, 'observation_id': observation_id}

    class Client:
        def get_cpu_worker_dispatch(self, project, operation):
            return {'state': 'terminal', 'latest_observation': latest}

        def settle_cpu_worker_dispatch(self, project, operation, *, observation_id):
            assert observation_id == latest['observation_id']
            return {'state': 'settled'}

        def observe_cpu_worker_dispatch(self, *_args):
            pytest.fail('terminal observation replay must not append a new observation')

    class Manager:
        def observe(self, unit):
            return SimpleNamespace(unit=unit, phase='completed', result='success', exit_status=0,
                                   launch_nonce=prepared.launch_nonce, invocation_id=latest['invocation_id'])

    monkeypatch.setattr(bridge.socket, 'gethostname', lambda: 'test-host')
    monkeypatch.setattr(bridge, '_controller_pin',
                        lambda _plan, _digest: (prepared.controller_head,
                                                prepared.controller_source_digest,
                                                prepared.controller_profile_digest))
    result = bridge.reconcile_worker(Client(), Manager(), prepared)
    assert result == {'observed': True, 'settled': True, 'state': 'settled'}
