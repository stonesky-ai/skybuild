"""Bounded proof contract checks without a database."""
from datetime import datetime, timedelta, timezone

import pytest
from skybuild.contracts import DomainError
from skybuild.cpu_worker_recovery import validate_proof


def _proof():
    return dict(host_id='wonko', unit_name='skybuild-job-' + 'a' * 24 + '.service',
                launch_nonce='a' * 32, controller_unit='controller.service', controller_invocation_id='b' * 32,
                launcher_stopped=True, launcher_cgroup_empty=True, launcher_fenced=True,
                unit_absent=True, container_absent=True, manifest_absent=True,
                fence_sha256='c' * 64, evidence_sha256='d' * 64,
                observed_at=datetime.now(timezone.utc).isoformat())


def test_current_complete_proof_is_valid():
    assert validate_proof(_proof()).tzinfo is not None


@pytest.mark.parametrize('delta', [-601, 60])
def test_stale_or_future_proof_is_not_admission(delta):
    proof = _proof()
    proof['observed_at'] = (datetime.now(timezone.utc) + timedelta(seconds=delta)).isoformat()
    with pytest.raises(DomainError, match='stale or in the future'):
        validate_proof(proof)


@pytest.mark.parametrize('change', ['missing', 'extra', 'truthy'])
def test_exact_proof_schema_and_true_booleans(change):
    proof = _proof()
    if change == 'missing':
        del proof['launcher_fenced']
    elif change == 'extra':
        proof['success'] = True
    else:
        proof['launcher_fenced'] = 1
    with pytest.raises(DomainError):
        validate_proof(proof)
