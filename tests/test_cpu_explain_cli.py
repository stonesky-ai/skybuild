"""Offline boundaries for authenticated, launch-free CPU eligibility inspection."""
import json
from pathlib import Path

import pytest
import skybuild

from skybuild.__main__ import main
from skybuild.contracts import DomainError

assert Path(skybuild.__file__).resolve().parents[2] == Path(__file__).resolve().parents[1]


ARGS = ["cpu-explain", "project", "task", "--action-id", "action", "--attempt-id", "attempt",
        "--units", "2", "--expected-revision", "3", "--readiness-generation", "4",
        "--claim-fence", "5", "--generation", "6", "--local-generation", "7"]


@pytest.fixture
def inspection(monkeypatch):
    calls = []
    principal = object()
    result = dict(eligible=True, outcome="eligible", reasons=[], reservation_state=None,
                  physical_dispatch_authorized=False, snapshot_only=True)
    # Never read process credentials or open a database/client in these tests.
    configuration = dict(SKYBUILD_DSN="synthetic-dsn", SKYBUILD_EXPECTED_DATABASE="skybuild_test",
                         SKYBUILD_TOKEN="synthetic-token")
    monkeypatch.setattr("skybuild.__main__._environment", configuration.__getitem__)
    monkeypatch.setattr("skybuild.__main__.Client", lambda *a, **k: pytest.fail("No HTTP client is needed"))

    class InspectionStore:
        # No migration, reservation, cancellation or control mutation interface.
        def __init__(self, dsn, database):
            assert (dsn, database) == ("synthetic-dsn", "skybuild_test")
            calls.append("store")

        def authenticate(self, token):
            assert token == "synthetic-token"
            calls.append("authenticate")
            return principal

        def explain_cpu(self, *args):
            assert args == (principal, "project", "task", "action", "attempt", 2, 3, 4, 5, 6, 7)
            calls.append("explain")
            return result

    monkeypatch.setattr("skybuild.store.Store", InspectionStore)
    return calls, result, InspectionStore


@pytest.mark.parametrize("outcome", ["eligible", "denied", "replay"])
def test_cli_preserves_snapshot_denial_and_replay(inspection, capsys, outcome):
    calls, result, _ = inspection
    result.update(eligible=outcome == "eligible", outcome=outcome)
    if outcome == "denied":
        result["reasons"] = [{"code": "control_conflict", "message": "Controls stale", "status_code": 409}]
    if outcome == "replay":
        result["reservation_state"] = "cancelled"
    assert main(ARGS) == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == result
    assert output.err == ""
    assert calls == ["store", "authenticate", "explain"]


@pytest.mark.parametrize("stage,code", [("authenticate", "authentication"), ("explain_cpu", "authorization"),
                                       ("explain_cpu", "authority"), ("explain_cpu", "validation"),
                                       ("explain_cpu", "unavailable")])
def test_cli_failure_never_prints_snapshot_or_exception_secrets(inspection, monkeypatch, capsys, stage, code):
    calls, _, store_type = inspection
    def fail(*args):
        raise DomainError(code, "synthetic-token synthetic-dsn", 403)
    monkeypatch.setattr(store_type, stage, fail)
    assert main(ARGS) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "Operation failed; verify configuration and supplied inputs\n"
    assert "explain" not in calls
    if stage == "authenticate":
        assert calls == ["store"]


@pytest.mark.parametrize("option", ["--action-id", "--attempt-id", "--units", "--expected-revision",
                                    "--readiness-generation", "--claim-fence", "--generation", "--local-generation"])
def test_cli_requires_every_identity_and_generation_before_access(inspection, option):
    args = ARGS.copy()
    index = args.index(option)
    del args[index:index + 2]
    with pytest.raises(SystemExit) as error:
        main(args)
    assert error.value.code == 2
    assert inspection[0] == []


def test_cli_rejects_noninteger_before_access(inspection):
    args = ARGS.copy()
    args[args.index("--units") + 1] = "true"
    with pytest.raises(SystemExit) as error:
        main(args)
    assert error.value.code == 2
    assert inspection[0] == []


def test_cli_missing_configuration_does_not_fall_back(inspection, monkeypatch, capsys):
    def missing(name):
        raise ValueError("Missing configuration")
    monkeypatch.setattr("skybuild.__main__._environment", missing)
    assert main(ARGS) == 1
    assert inspection[0] == []
    assert capsys.readouterr().out == ""
