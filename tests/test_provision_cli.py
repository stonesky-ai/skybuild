"""Explicit principal provisioning uses the Store operation contract."""
import io

import pytest

from skybuild.__main__ import main
from skybuild.store import OPERATIONS


@pytest.fixture
def provisioning(monkeypatch):
    calls = []
    monkeypatch.setenv('SKYBUILD_DSN', 'synthetic-dsn')
    monkeypatch.setenv('SKYBUILD_EXPECTED_DATABASE', 'skybuild_test')
    monkeypatch.delenv('SKYBUILD_TOKEN', raising=False)
    monkeypatch.setattr('sys.stdin', io.StringIO('synthetic-private-token-with-32-characters\n'))

    class ProvisionStore:
        def __init__(self, dsn, database):
            assert (dsn, database) == ('synthetic-dsn', 'skybuild_test')

        def provision_principal(self, principal, token, *, is_admin, grants):
            calls.append((principal, token, is_admin, grants))

    monkeypatch.setattr('skybuild.store.Store', ProvisionStore)
    return calls


def test_explicit_petri_worker_profile_can_be_provisioned_without_implicit_admin(provisioning, capsys):
    args = ['provision', 'smoke_worker', '--token-stdin']
    for operation in sorted(OPERATIONS):
        args += ['--grant', 'skybuild:' + operation]
    assert main(args) == 0
    assert provisioning == [('smoke_worker', 'synthetic-private-token-with-32-characters',
                             False, {'skybuild': set(OPERATIONS)})]
    output = capsys.readouterr()
    assert output.out == 'Principal provisioned\n' and output.err == ''


@pytest.mark.parametrize('grant', ['skybuild:tasks:launch', 'skybuild:tasks:admin',
                                  'skybuild:cord:write', 'skybuild:claim', ':tasks:claim'])
def test_unknown_or_malformed_grants_do_not_provision(provisioning, capsys, grant):
    assert main(['provision', 'smoke_worker', '--token-stdin', '--grant', grant]) == 1
    assert provisioning == []
    output = capsys.readouterr()
    assert output.out == '' and 'synthetic' not in output.err


def test_legacy_profile_is_not_implicitly_broadened(provisioning):
    assert main(['provision', 'legacy_worker', '--token-stdin',
                 '--grant', 'skybuild:tasks:read']) == 0
    assert provisioning[0][2:] == (False, {'skybuild': {'tasks:read'}})
