"""Bounded HTTP workflow input and authenticated Store delegation."""

from fastapi.testclient import TestClient
import pytest

from skybuild.api import create_app
from skybuild.contracts import DomainError, Principal
from skybuild.workflow import ValidationResult, ValidationStage, ResultState


class WorkflowStore:
    def __init__(self):
        self.calls = []

    def authenticate(self, token):
        if token != "valid":
            raise DomainError("authentication", "Credential detail", 401)
        return Principal("worker", False, {"project": frozenset({"tasks:read", "tasks:write"})})

    def _record(self, method, actor, project, task, *args):
        if project not in actor.grants:
            raise DomainError("authorization", "Project operation not permitted", 403)
        self.calls.append((method, actor, project, task, args))
        return {"project_id": project, "task_id": task, "revision": 8, "place": "hold",
                "validation": [], "enabled_actions": ["release_hold"], "evidence_freshness": "current"}

    def workflow_transition(self, *args):
        return self._record("transition", *args)

    def initialize_workflow(self, *args):
        return self._record("initialize", *args)

    def task_workflow(self, *args):
        return self._record("read", *args)


@pytest.fixture
def api():
    store = WorkflowStore()
    with TestClient(create_app(store), raise_server_exceptions=False) as client:
        client.headers.update({"Authorization": "Bearer valid", "If-Match": "7", "Idempotency-Key": "operation-1"})
        yield client, store


PATH = "/api/v1/projects/project/tasks/TASK-1/workflow"


def test_transition_forwards_headers_and_authenticated_identity_only(api):
    client, store = api
    response = client.post(PATH, json={"event": "hold", "reason": "Wait for required access"})
    assert response.status_code == 200
    method, actor, project, task, args = store.calls[-1]
    assert method == "transition"
    assert actor.principal_id == "worker"
    assert (project, task) == ("project", "TASK-1")
    assert args == ("hold", {"reason": "Wait for required access"}, 7, "operation-1")
    assert response.json()["place"] == "hold"


def test_initialize_has_no_caller_state_and_read_preserves_projection(api):
    client, store = api
    assert client.post(PATH, json={"event": "initialize"}).status_code == 200
    assert store.calls[-1][-1] == (7, "operation-1")
    response = client.get(PATH)
    assert response.status_code == 200
    assert response.json()["enabled_actions"] == ["release_hold"]
    assert store.calls[-1][0] == "read"


@pytest.mark.parametrize("body", [
    {"event": "teleport"}, {"event": "claim", "context": {"claim_live": True}},
    {"event": "claim", "place": "done"}, {"event": "claim", "expected_revision": 7},
    {"event": "claim", "operation_id": "forged"}, {"event": "claim", "reason": "unexpected"},
    {"event": "hold"}, {"event": "hold", "reason": None},
    {"event": "hold", "reason": "bad\x00text"},
    {"event": "hold", "reason": "x" * 4097},
    {"event": "hold", "reason": "Wait", "until": "2030-01-01T00:00:00+00:00"},
    {"event": "defer", "reason": "Wait"},
    {"event": "defer", "reason": "Wait", "until": "2030-01-01T00:00:00"},
    {"event": "defer", "reason": "Wait", "until": "2030-01-01T00:00:00+00:00", "milestone_task_id": "T"},
    {"event": "validation_result"}, {"event": "validation_result", "result": {}},
    {"event": "initialize", "reason": "caller initialization"},
    {"event": "freeze", "policy_reason": "Skip publication"},
])
def test_invalid_event_fails_before_store(api, body):
    client, store = api
    assert client.post(PATH, json=body).status_code == 422
    assert store.calls == []


def test_validation_result_is_strict_but_authority_stays_with_store(api):
    client, store = api
    result = ValidationResult("project", "TASK-1", ValidationStage.SCANS, ResultState.PASSED).to_dict()
    assert client.post(PATH, json={"event": "validation_result", "result": result}).status_code == 200
    assert store.calls[-1][-1][1] == {"result": result}
    store.calls.clear()
    for changed in ({"input_generation": True}, {"producer_authorized": True},
                    {"parameters": [["name", "bad\x00value"]]}, {"state": "approved"}):
        assert client.post(PATH, json={"event": "validation_result", "result": {**result, **changed}}).status_code == 422
        assert not store.calls


@pytest.mark.parametrize("header,value,status", [
    ("Authorization", "Bearer invalid", 401), ("If-Match", "0", 422),
    ("If-Match", "true", 422), ("Idempotency-Key", "bad/key", 422),
])
def test_workflow_header_bounds_and_authentication(api, header, value, status):
    client, store = api
    assert client.post(PATH, headers={header: value}, json={"event": "claim"}).status_code == status
    assert not store.calls


def test_scope_is_checked_by_store_for_reads_and_writes(api):
    client, store = api
    path = PATH.replace("/project/", "/other/")
    assert client.get(path).status_code == 403
    assert client.post(path, json={"event": "claim"}).status_code == 403
    assert not store.calls


@pytest.mark.parametrize("body", [
    {"place": "done"}, {"validation": []}, {"enabled_actions": []}, {"evidence_freshness": "current"},
    {"metadata": {"_skybuild_workflow": {"petri": {"token": {"place": "done"}}}}},
    {"metadata": {"_skybuild_completion": {}}},
])
def test_computed_and_reserved_task_writes_fail_before_store(api, body):
    client, store = api
    path = PATH.removesuffix("/workflow")
    assert client.patch(path, json=body).status_code == 422
    assert client.post(path.rsplit("/", 1)[0], json={"task_id": "T", "title": "Title", "description": "Brief", **body}).status_code == 422
    assert not store.calls
