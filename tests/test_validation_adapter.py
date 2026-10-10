from dataclasses import replace
import json

import httpx
import pytest

from skybuild.client import Client, ClientError
from skybuild.validation_adapter import (ValidationAdapterError, _read_result,
                                         main, record_validation_result)
from skybuild.workflow import ResultState, ValidationResult, ValidationStage


@pytest.fixture
def result():
    return ValidationResult("sample-project", "TASK-1", ValidationStage.UNIT_TESTS,
                            ResultState.PASSED, attempt_id="attempt-1", claim_fence=7,
                            source_head="a" * 40, target_base="b" * 40,
                            definition_revision=3, input_generation=4,
                            policy_version="policy-1", producer="checker", check_id="pytest",
                            tool_version="8.0", parameters=(("profile", "unit"),),
                            artifacts=("artifact/check-1",))


@pytest.mark.parametrize("stage", list(ValidationStage))
@pytest.mark.parametrize("state", [ResultState.RUNNING, ResultState.PASSED, ResultState.FAILED])
def test_all_stages_use_guarded_endpoint_without_launch(result, stage, state):
    packet = replace(result, stage=stage, state=state, findings=("Correction required",) if state == ResultState.FAILED else ())
    requests = []
    def handler(request):
        requests.append(request)
        assert request.url.path == "/api/v1/projects/sample-project/tasks/TASK-1/workflow"
        assert request.headers["If-Match"] == "9"
        assert request.headers["Idempotency-Key"] == "operation-1"
        assert json.loads(request.content) == {"event": "validation_result", "result": packet.to_dict()}
        return httpx.Response(200, json={"token": {"place": "ready" if state == ResultState.FAILED else "validating"}})
    with Client("https://service.example", "test-token", transport=httpx.MockTransport(handler)) as client:
        response = record_validation_result(client, packet, expected_revision=9, idempotency_key="operation-1")
    assert len(requests) == 1
    assert response["token"]["place"] == ("ready" if state == ResultState.FAILED else "validating")


@pytest.mark.parametrize("status,code", [(409, "workflow_conflict"), (403, "forbidden")])
def test_stale_or_unauthorized_result_does_not_refresh_or_retry(result, status, code):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(status, json={"error": {"code": code, "message": "Result rejected"}})
    with Client("https://service.example", "test-token", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ClientError):
            record_validation_result(client, result, expected_revision=9, idempotency_key="original-operation")
    assert len(requests) == 1


@pytest.mark.parametrize("change", [
    {"attempt_id": None}, {"claim_fence": None}, {"claim_fence": 0},
    {"source_head": "branch-name"}, {"target_base": None}, {"producer": ""},
    {"policy_version": ""}, {"check_id": ""}, {"tool_version": ""},
    {"state": ResultState.STALE}, {"state": ResultState.NOT_APPLICABLE},
])
def test_incomplete_packets_make_no_request(result, change):
    class NoCalls:
        def workflow_transition(self, *args, **kwargs):
            pytest.fail("Invalid result reached the network")
    with pytest.raises(ValidationAdapterError):
        record_validation_result(NoCalls(), replace(result, **change), expected_revision=9,
                                 idempotency_key="operation-1")


@pytest.mark.parametrize("revision,key", [(True, "op"), (0, "op"), (9, ""), (9, "bad/key")])
def test_explicit_revision_and_stable_operation_required(result, revision, key):
    with pytest.raises(ValidationAdapterError):
        record_validation_result(None, result, expected_revision=revision, idempotency_key=key)


def test_not_applicable_keeps_policy_reason(result):
    packet = replace(result, state=ResultState.NOT_APPLICABLE, policy_reason="No long tests for this project")
    class Capture:
        def workflow_transition(self, project, task, event, body, **kwargs):
            assert body["result"]["policy_reason"] == packet.policy_reason
            return {"recorded": True}
    assert record_validation_result(Capture(), packet, expected_revision=9, idempotency_key="operation-1") == {"recorded": True}


def test_safe_file_round_trip_and_refusals(result, tmp_path):
    path = tmp_path / "result.json"
    path.write_text(json.dumps(result.to_dict()))
    assert _read_result(path) == result
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(OSError):
        _read_result(link)
    path.write_bytes(b" " * 16385)
    with pytest.raises(ValidationAdapterError):
        _read_result(path)


def test_cli_rejects_bad_packet_before_credentials_or_network(tmp_path, capsys):
    path = tmp_path / "result.json"
    path.write_text("invalid secret packet")
    assert main(["--url", "https://unused.ts.net", "--token-file", str(tmp_path / "absent"),
                 "--result", str(path), "--revision", "9", "--operation-id", "op"]) == 2
    captured = capsys.readouterr()
    assert "Preserve the result" in captured.err
    assert "secret" not in captured.err


@pytest.mark.parametrize("rejected", [False, True])
def test_cli_keeps_packet_and_operation_identity(result, tmp_path, monkeypatch, capsys, rejected):
    from skybuild import validation_adapter as adapter
    path = tmp_path / "result.json"
    original = json.dumps(result.to_dict())
    path.write_text(original)
    monkeypatch.setattr(adapter, "_private_endpoint", lambda url, resolve: url)
    monkeypatch.setattr(adapter, "_token_from_file", lambda path: "private-token")
    calls = []
    class FakeClient:
        def __init__(self, url, token, **kwargs):
            assert kwargs == {"retries": 0, "timeout": 5, "trust_env": False, "ca_file": None}
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def workflow_transition(self, project, task, event, body, **kwargs):
            calls.append((body, kwargs))
            if rejected:
                raise ClientError("forbidden", "private remote diagnostic", 403)
            return {"recorded": True}
    monkeypatch.setattr(adapter, "Client", FakeClient)
    status = main(["--url", "https://service.ts.net", "--token-file", str(tmp_path / "token"),
                   "--result", str(path), "--revision", "9", "--operation-id", "original-operation"])
    assert status == (2 if rejected else 0)
    assert calls == [({"result": result.to_dict()}, {"expected_revision": 9, "idempotency_key": "original-operation"})]
    assert path.read_text() == original
    output = capsys.readouterr()
    assert "private-token" not in output.err + output.out
    assert "private remote diagnostic" not in output.err + output.out
