"""Client workflow routing and stable request identity across transport retry."""

import json

import httpx
import pytest

from skybuild.client import Client, ClientError


def test_workflow_client_preserves_body_revision_and_operation_on_retry():
    requests = []
    def handle(request):
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ConnectError("lost", request=request)
        return httpx.Response(200, json={"task_id": "TASK-1", "revision": 8, "place": "hold"})
    with Client("http://service", "token", transport=httpx.MockTransport(handle)) as client:
        answer = client.workflow_transition("project", "TASK-1", "hold", {"reason": "Wait"},
                                            expected_revision=7, idempotency_key="stable-key")
    assert answer["place"] == "hold"
    assert len(requests) == 2
    assert requests[0].content == requests[1].content
    assert requests[0].headers["If-Match"] == requests[1].headers["If-Match"] == "7"
    assert requests[0].headers["Idempotency-Key"] == requests[1].headers["Idempotency-Key"] == "stable-key"
    assert requests[0].url.path.endswith("/tasks/TASK-1/workflow")


def test_workflow_client_read_and_initialize():
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"place": "ready"})
    with Client("http://service", "token", transport=httpx.MockTransport(handle)) as client:
        assert client.task_workflow("project", "TASK-1")["place"] == "ready"
        client.workflow_transition("project", "TASK-1", "initialize", expected_revision=1)
    assert requests[0].method == "GET"
    assert json.loads(requests[1].content) == {"event": "initialize"}


def test_workflow_client_does_not_retry_revision_conflict():
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(409, json={"error": {"code": "workflow_conflict", "message": "Task revision changed"}})
    with Client("http://service", "token", transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ClientError) as error:
            client.workflow_transition("project", "TASK-1", "submit", expected_revision=3)
    assert error.value.status_code == 409
    assert len(requests) == 1


@pytest.mark.parametrize("event,body", [("teleport", None), ("hold", {"event": "accept"}), ("hold", [])])
def test_client_rejects_unknown_or_replaced_event_without_request(event, body):
    def handle(request):
        raise AssertionError("Unexpected request")
    with Client("http://service", "token", transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ValueError):
            client.workflow_transition("project", "TASK-1", event, body, expected_revision=1)



def test_claim_client_uses_atomic_claim_endpoint():
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"fence": 1, "task_revision": 8})
    with Client("http://service", "token", transport=httpx.MockTransport(handle)) as client:
        claim = client.claim_task("project", "TASK-1", expected_revision=7, lease_seconds=30,
                                  idempotency_key="claim-key")
        assert claim["task_revision"] == 8
        with pytest.raises(ValueError):
            client.claim_task("project", "TASK-1", expected_revision=7, lease_seconds=True)
    assert len(requests) == 1
    assert requests[0].url.path.endswith("/tasks/TASK-1/claim")
    assert json.loads(requests[0].content) == {"lease_seconds": 30}
    assert requests[0].headers["If-Match"] == "7"
    assert requests[0].headers["Idempotency-Key"] == "claim-key"
