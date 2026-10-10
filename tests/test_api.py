import importlib
import json

import pytest
from fastapi.testclient import TestClient

from skybuild.api import MAX_BODY_BYTES, create_app
from skybuild.contracts import DomainError, Principal


class FakeStore:
    def __init__(self):
        self.calls = []
        self.available = True

    def authenticate(self, token):
        if token != "valid-token":
            raise DomainError("bad_token", "SECRET credential detail", 401)
        return Principal("worker", False, {"project": frozenset({"tasks:read", "tasks:write"})})

    def readiness(self):
        if not self.available:
            raise RuntimeError("postgresql://SECRET")
        return {"ready": True, "dsn": "SECRET"}

    def __getattr__(self, name):
        def operation(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if args[1] == "forbidden":
                raise DomainError("forbidden", "Project access denied", 403)
            if name == "update_task" and args[-2] != 7:
                raise DomainError("stale_revision", "Task revision changed", 409)
            if name in {"list_tasks", "task_history", "inbox"}:
                return [{"project_id": args[1]}]
            return {"project_id": args[1], "revision": 7}
        return operation


@pytest.fixture
def api():
    store = FakeStore()
    with TestClient(create_app(store), raise_server_exceptions=False) as client:
        client.headers.update({"Authorization": "Bearer valid-token", "Idempotency-Key": "operation-key"})
        yield client, store


def test_health_import_and_factory_have_no_store_side_effects(api):
    client, store = api
    assert not store.calls
    assert client.get("/").status_code == 200
    assert client.get("/version").json()["protocol"] == "v1"
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").json() == {"status": "ready"}
    store.available = False
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert "SECRET" not in response.text
    importlib.reload(importlib.import_module("skybuild.api"))
    assert not store.calls


def test_whoami_reports_only_authenticated_identity_and_grants(api):
    client, store = api
    assert client.get("/api/v1/me").json() == {
        "principal_id": "worker", "is_admin": False,
        "grants": {"project": ["tasks:read", "tasks:write"]},
    }
    assert not store.calls
    assert client.get("/api/v1/me", headers={"Authorization": "Bearer bad-token"}).status_code == 401


@pytest.mark.parametrize("authorization", [None, "Basic valid-token", "Bearer bad-token", "Bearer one two"])
def test_auth_failures_are_generic(api, authorization):
    client, store = api
    client.headers.pop("Authorization")
    headers = {"Authorization": authorization} if authorization else {}
    response = client.get("/api/v1/projects/project/tasks", headers=headers)
    assert response.status_code == 401
    assert response.json() == {"error": {"code": "unauthenticated", "message": "Authentication required"}}
    assert not store.calls


def test_task_contract_and_revision_forwarding(api):
    client, store = api
    base = "/api/v1/projects/project/tasks"
    response = client.post(base, json={"task_id": "TASK-1", "title": "Title", "description": "Full brief"})
    assert response.status_code == 201
    assert store.calls[-1][1][0].principal_id == "worker"
    assert store.calls[-1][1][-1] == "operation-key"
    response = client.patch(base + "/TASK-1", headers={"If-Match": "7"}, json={"status": "ready"})
    assert response.status_code == 200
    assert store.calls[-1][1][-2:] == (7, "operation-key")
    assert client.patch(base + "/TASK-1", headers={"If-Match": "6"}, json={"status": "ready"}).status_code == 409
    assert client.get("/api/v1/projects/forbidden/tasks").status_code == 403


@pytest.mark.parametrize("body", [
    {"task_id": "TASK-1", "title": "", "description": "brief"},
    {"task_id": "TASK-1", "title": "Title", "description": "brief", "is_admin": True},
    {"task_id": "TASK-1", "title": "Title", "description": "brief", "priority": True},
    {"task_id": "TASK-1", "title": "Title", "description": "brief", "metadata": {"text": "x" * 16_384}},
])
def test_create_rejects_invalid_fields_before_mutation(api, body):
    client, store = api
    assert client.post("/api/v1/projects/project/tasks", json=body).status_code == 422
    assert not store.calls


@pytest.mark.parametrize("identifier", [".", "..", "part/part", "part\\part", "part%2Fpart", "part\x00part", "part\x85part"])
def test_create_rejects_unaddressable_identifiers(api, identifier):
    client, store = api
    response = client.post("/api/v1/projects/project/tasks", json={"task_id": identifier, "title": "T", "description": "D"})
    assert response.status_code == 422
    assert not store.calls


@pytest.mark.parametrize("revision", [None, '"7"', "W/7", "-1", "0", "1.2", "9" * 20])
def test_update_requires_numeric_revision(api, revision):
    client, store = api
    headers = {"If-Match": revision} if revision else {}
    assert client.patch("/api/v1/projects/project/tasks/TASK-1", headers=headers, json={"title": "new"}).status_code == 422
    assert not store.calls


def test_missing_key_unknown_patch_and_pagination(api):
    client, store = api
    client.headers.pop("Idempotency-Key")
    base = "/api/v1/projects/project/tasks"
    assert client.post(base, json={"task_id": "A", "title": "T", "description": "D"}).status_code == 422
    client.headers["Idempotency-Key"] = "key"
    assert client.patch(base + "/A", headers={"If-Match": "7"}, json={"task_id": "B"}).status_code == 422
    for query in ("limit=0", "limit=101", "offset=-1"):
        assert client.get(base + "?" + query).status_code == 422
    assert not store.calls
    assert client.get(base + "?limit=3&offset=4").status_code == 200
    assert store.calls[-1][2] == {"limit": 3, "offset": 4, "after_task_id": None, "by_id": False}


def test_cord_actor_action_and_reply_contract(api):
    client, store = api
    base = "/api/v1/projects/project/cord"
    assert client.post(base + "/messages", json={"recipient": "other", "subject": "S", "body": "B"}).status_code == 201
    assert store.calls[-1][0] == "send_message"
    assert client.get(base + "/inbox").status_code == 200
    for action in ("receipt", "handle", "reply"):
        body = {"subject": "Reply", "body": "Text", "handle_original": True} if action == "reply" else {}
        assert client.post(base + "/messages/MSG-1/" + action, json=body).status_code == 200
        assert store.calls[-1][1][3] == action
    assert client.post(base + "/messages", json={"recipient": "other", "sender": "admin", "subject": "S", "body": "B"}).status_code == 422
    assert client.post(base + "/messages/MSG-1/handle", json={"recipient": "other"}).status_code == 422


def test_streaming_body_limit_and_generic_failure(api):
    client, store = api
    def body():
        for _ in range(5):
            yield b"x" * (MAX_BODY_BYTES // 4)
    assert client.post("/api/v1/projects/project/tasks", content=body()).status_code == 413
    assert not store.calls
    def fail(*args, **kwargs):
        raise RuntimeError("SECRET DSN")
    store.list_tasks = fail
    response = client.get("/api/v1/projects/project/tasks")
    assert response.status_code == 503
    assert "SECRET" not in response.text


@pytest.mark.parametrize("depth", [65, 1000])
def test_excessively_nested_json_is_validation_failure_before_mutation(api, depth):
    client, store = api
    body = '{"task_id":"T","title":"Title","description":"Brief","metadata":{"nested":' + "[" * depth + "0" + "]" * depth + "}}"
    assert len(body.encode()) < MAX_BODY_BYTES
    response = client.post("/api/v1/projects/project/tasks", content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    assert response.json() == {"error": {"code": "validation", "message": "Invalid request"}}
    assert not store.calls


def test_json_depth_guard_ignores_escaped_text_and_preserves_store_errors(api):
    client, store = api
    description = json.dumps({"quoted": '\\"' + "[" * 1000 + "{" * 1000})
    assert client.post("/api/v1/projects/project/tasks", json={"task_id": "T", "title": "Title", "description": description}).status_code == 201
    def fail(*args, **kwargs):
        raise RecursionError("SECRET Store failure")
    store.create_task = fail
    response = client.post("/api/v1/projects/project/tasks", json={"task_id": "T2", "title": "Title", "description": "Brief"})
    assert response.status_code == 503
    assert "SECRET" not in response.text


def test_no_launch_or_admin_http_routes(api):
    client, _ = api
    paths = client.get("/openapi.json").json()["paths"]
    assert not any(any(word in path for word in ("launch", "provision", "migrate", "execute")) for path in paths)


def test_task_usage_routes_validate_exact_decimal_and_use_store(api):
    import uuid

    client, store = api
    path = "/api/v1/projects/skybuild/tasks/task-a/usage-history"
    body = {
        "event_kind": "consumed", "attempt_id": "attempt-1", "task_revision": 1,
        "definition_revision": 1, "input_generation": 1, "claim_fence": 1,
        "provider": "provider-x", "model": "model-x", "pool_id": "pool-x",
        "policy_window_id": "window-x", "operation_id": "request-1",
        "unit": "provider-units", "quantity": "0.125",
        "evidence_ref": "receipt:request-1", "evidence_sha256": "a" * 64,
        "reason": "Record known usage",
    }
    invalid = client.post(path, json={**body, "quantity": "01"})
    assert invalid.status_code == 422
    assert not store.calls

    recorded = client.post(path, json=body)
    assert recorded.status_code == 201
    assert store.calls[-1][0] == "record_task_usage"

    history = client.get(path)
    assert history.status_code == 200
    assert store.calls[-1][0] == "task_usage_history"

    event_id = str(uuid.uuid4())
    resolution = {
        "operation_id": "reconcile-1", "quantity": "0",
        "evidence_ref": "receipt:no-charge", "evidence_sha256": "b" * 64,
        "reason": "Provider evidence confirms no usage",
    }
    resolved = client.post(path + "/" + event_id + "/resolve", json=resolution)
    assert resolved.status_code == 201
    assert store.calls[-1][0] == "resolve_task_usage"
