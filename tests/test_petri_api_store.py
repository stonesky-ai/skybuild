"""Real HTTP, Store and kernel behavior on a disposable PostgreSQL database."""

from fastapi.testclient import TestClient

from skybuild.api import create_app
from test_store import actors, create, store


def test_http_workflow_round_trip_revision_replay_and_legacy_fields(store, actors):
    project, people = actors
    task = create(store, people["owner"], project, "http-petri", acceptance_criteria=["Preserve task history"])
    task = store.task_action(people["owner"], project, task["task_id"], "ready", {"reason": "Definition complete"},
                             task["revision"], "http-ready")
    path = "/api/v1/projects/{}/tasks/{}".format(project, task["task_id"])
    with TestClient(create_app(store)) as client:
        client.headers["Authorization"] = "Bearer " + people["owner_token"]
        response = client.post(path + "/workflow", json={"event": "initialize"},
                               headers={"If-Match": str(task["revision"]), "Idempotency-Key": "http-initialize"})
        assert response.status_code == 200
        initialized = response.json()
        assert initialized["token"]["place"] == "ready"
        assert initialized["task"]["title"] == task["title"]
        assert initialized["task"]["revision"] == task["revision"] + 1
        headers = {"If-Match": str(initialized["task"]["revision"]), "Idempotency-Key": "http-hold"}
        held_response = client.post(path + "/workflow", json={"event": "hold", "reason": "Wait for access"}, headers=headers)
        assert held_response.status_code == 200
        held = held_response.json()
        assert held["token"]["place"] == "hold"
        assert client.post(path + "/workflow", json={"event": "hold", "reason": "Wait for access"}, headers=headers).json() == held
        assert client.post(path + "/workflow", json={"event": "hold", "reason": "Other reason"}, headers=headers).status_code == 409
        assert client.post(path + "/workflow", json={"event": "release_hold", "reason": "Access restored"},
                           headers={**headers, "Idempotency-Key": "http-stale"}).status_code == 409
        released = client.post(path + "/workflow", json={"event": "release_hold", "reason": "Access restored"},
                               headers={"If-Match": str(held["task"]["revision"]), "Idempotency-Key": "http-release"})
        assert released.status_code == 200
        assert released.json()["token"]["place"] == "ready"
        assert released.json()["token"]["attempt_id"] is None
        detail = client.get(path).json()
        assert detail["place"] == "ready"
        assert detail["status"] == "ready"
        assert detail["title"] == task["title"]
        assert detail["enabled_actions"] == []
        listed = client.get(f"/api/v1/projects/{project}/tasks").json()
        assert next(item for item in listed if item["task_id"] == task["task_id"])["place"] == "ready"
        workflow = client.get(path + "/workflow").json()
        assert workflow["token"]["task_id"] == task["task_id"]
        assert "hold" in workflow["available_actions"]
        history = client.get(path + "/history").json()
        assert {"workflow_initialized", "workflow.hold", "workflow.release_hold"} <= {item["operation"] for item in history}


def test_public_workflow_cannot_invent_producer_or_publication_facts(store, actors):
    project, people = actors
    task = create(store, people["owner"], project, "http-guards")
    view = store.initialize_workflow(people["owner"], project, task["task_id"], task["revision"], "guard-initialize")
    path = "/api/v1/projects/{}/tasks/{}/workflow".format(project, task["task_id"])
    with TestClient(create_app(store)) as client:
        client.headers.update({"Authorization": "Bearer " + people["owner_token"],
                               "If-Match": str(view["task"]["revision"]), "Idempotency-Key": "forged"})
        assert client.post(path, json={"event": "accept", "context": {"acceptance_verified": True}}).status_code == 422
        assert client.post(path, json={"event": "accept"}).status_code == 409
        assert client.post(path, json={"event": "claim"}).status_code == 409
        client.headers["Authorization"] = "Bearer " + people["outsider_token"]
        assert client.get(path).status_code == 403
