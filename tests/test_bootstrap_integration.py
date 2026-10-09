"""HTTP-to-PostgreSQL checks against an explicitly supplied disposable database."""

import os
import socket
import subprocess
import sys
import time
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict

from skybuild.api import create_app
from skybuild.store import Store


@pytest.fixture
def service():
    dsn = os.environ.get("SKYBUILD_HTTP_TEST_DSN")
    if not dsn:
        pytest.skip("Set SKYBUILD_HTTP_TEST_DSN to a task-owned disposable database")
    database = conninfo_to_dict(dsn).get("dbname", "")
    if not database.startswith("skybuild_") or not database.endswith("_test"):
        pytest.fail("HTTP integration checks require a named skybuild_*_test database")
    store = Store(dsn, database)
    store.migrate()
    project = "http-" + uuid4().hex
    owner, worker = "owner-" + uuid4().hex, "worker-" + uuid4().hex
    owner_token, worker_token = uuid4().hex + uuid4().hex, uuid4().hex + uuid4().hex
    store.provision_principal(owner, owner_token, is_admin=True)
    store.provision_principal(worker, worker_token, grants={project: [
        "tasks:read", "tasks:write", "cord:send", "cord:read", "cord:handle",
    ]})
    with TestClient(create_app(store), raise_server_exceptions=False) as client:
        yield client, store, project, owner, worker, owner_token, worker_token


def headers(token, key=None, revision=None):
    result = {"Authorization": "Bearer " + token}
    if key is not None:
        result["Idempotency-Key"] = key
    if revision is not None:
        result["If-Match"] = str(revision)
    return result


def test_http_task_revision_replay_and_scope(service):
    client, _, project, _, _, owner_token, worker_token = service
    base = f"/api/v1/projects/{project}/tasks"
    body = {"task_id": "feature", "title": "Preserve Unicode", "description": "Résumé — Δ"}
    created = client.post(base, json=body, headers=headers(owner_token, "create"))
    assert created.status_code == 201, created.text
    task = created.json()
    assert task["revision"] == 1
    assert task["description"] == body["description"]
    assert client.get(base, headers=headers(worker_token)).status_code == 200
    assert client.get(base.replace(project, "unassigned"), headers=headers(worker_token)).status_code == 403
    assert client.get(base).status_code == 401

    updated = client.patch(base + "/feature", json={"title": "Changed"}, headers=headers(worker_token, "update", 1))
    assert updated.status_code == 200, updated.text
    assert updated.json()["revision"] == 2
    replay = client.post(base, json=body, headers=headers(owner_token, "create"))
    assert replay.json() == task
    stale = client.patch(base + "/feature", json={"title": "Stale"}, headers=headers(worker_token, "stale", 1))
    assert stale.status_code == 409
    conflict = client.post(base, json={**body, "title": "Different"}, headers=headers(owner_token, "create"))
    assert conflict.status_code == 409
    history = client.get(base + "/feature/history", headers=headers(owner_token))
    assert history.status_code == 200
    assert len(history.json()) == 2


def test_http_manual_task_action_preserves_reason_and_revision(service):
    client, _, project, _, _, owner_token, _ = service
    base = f"/api/v1/projects/{project}/tasks"
    created = client.post(base, json={"task_id": "action", "title": "Act", "description": "Review"},
                          headers=headers(owner_token, "action-create"))
    assert created.status_code == 201
    action = client.post(base + "/action/actions/rework",
                         json={"reason": "Check failed", "next_action": "Fix failed check"},
                         headers=headers(owner_token, "action-rework", 1))
    assert action.status_code == 200, action.text
    assert action.json()["phase"] == "needs-rework"
    assert action.json()["revision"] == 2
    assert client.post(base + "/action/actions/rework", json={"reason": "Check failed", "next_action": "Fix failed check"},
                       headers=headers(owner_token, "action-rework", 1)).json() == action.json()
    assert client.post(base + "/action/actions/defer", json={"reason": "Wait", "until": "2026-10-09T12:00:00"},
                       headers=headers(owner_token, "invalid-date", 2)).status_code == 422


def test_http_ready_requires_acceptance_and_does_not_start_work(service):
    client, _, project, _, _, owner_token, _ = service
    base = f"/api/v1/projects/{project}/tasks"
    created = client.post(base, json={"task_id": "ready-task", "title": "Ready task", "description": "Brief",
                                      "acceptance_criteria": ["Check result"]}, headers=headers(owner_token, "create-ready"))
    assert created.status_code == 201
    response = client.post(base + "/ready-task/actions/ready", json={"reason": "Definition reviewed"},
                           headers=headers(owner_token, "mark-ready", 1))
    assert response.status_code == 200
    assert (response.json()["status"], response.json()["phase"]) == ("ready", "ready-for-work")
    assert response.json()["next_action"] == "Await explicit admission and ownership"


def test_http_task_id_cursor_lists_next_page(service):
    client, _, project, _, _, owner_token, _ = service
    base = f"/api/v1/projects/{project}/tasks"
    for task_id in ("cursor-a", "cursor-b", "cursor-c"):
        assert client.post(base, json={"task_id": task_id, "title": task_id, "description": "Brief"},
                           headers=headers(owner_token, "create-" + task_id)).status_code == 201
    first = client.get(base + "?limit=2&by_id=true", headers=headers(owner_token))
    assert [task["task_id"] for task in first.json()] == ["cursor-a", "cursor-b"]
    second = client.get(base + "?limit=2&after_task_id=cursor-b", headers=headers(owner_token))
    assert [task["task_id"] for task in second.json()] == ["cursor-c"]


def test_http_split_exposes_lineage_and_refuses_stale_revision(service):
    client, _, project, _, _, owner_token, _ = service
    base = f"/api/v1/projects/{project}/tasks"
    created = client.post(base, json={"task_id": "parent", "title": "Parent", "description": "Two scopes",
                                      "acceptance_criteria": ["first", "second"]},
                          headers=headers(owner_token, "parent-create"))
    assert created.status_code == 201
    body = {"reason": "Separate independent scopes", "incoming": {}, "children": [
        {"task_id": "first", "title": "First", "description": "First scope", "acceptance_criteria": ["first"], "dependencies": []},
        {"task_id": "second", "title": "Second", "description": "Second scope", "acceptance_criteria": ["second"], "dependencies": []},
    ]}
    response = client.post(base + "/parent/split", json=body, headers=headers(owner_token, "parent-split", 1))
    assert response.status_code == 200, response.text
    assert response.json()["source"]["status"] == "superseded"
    assert client.post(base + "/parent/split", json=body, headers=headers(owner_token, "parent-split", 1)).json() == response.json()
    assert len(client.get(base + "/parent/lineage", headers=headers(owner_token)).json()) == 2
    assert client.post(base + "/parent/split", json=body, headers=headers(owner_token, "stale-split", 1)).status_code == 409


def test_http_merge_uses_all_expected_revisions(service):
    client, _, project, _, _, owner_token, _ = service
    base = f"/api/v1/projects/{project}/tasks"
    for task_id in ("left", "right"):
        created = client.post(base, json={"task_id": task_id, "title": task_id, "description": task_id,
                                          "acceptance_criteria": [task_id]},
                              headers=headers(owner_token, "create-" + task_id))
        assert created.status_code == 201
    body = {"reason": "Join scopes", "source_task_ids": ["left", "right"],
            "expected_revisions": {"left": 1, "right": 1}, "incoming_dependents": [],
            "target": {"task_id": "combined", "title": "Combined", "description": "Combined scope",
                       "acceptance_criteria": ["left", "right"], "dependencies": []}}
    response = client.post(base + "/merge", json=body, headers=headers(owner_token, "merge-key"))
    assert response.status_code == 200, response.text
    assert response.json()["target"]["task_id"] == "combined"
    assert client.post(base + "/merge", json=body, headers=headers(owner_token, "merge-key")).json() == response.json()
    assert len(client.get(base + "/combined/lineage", headers=headers(owner_token)).json()) == 2


def test_http_cord_handling_and_transactional_reply(service):
    client, _, project, owner, worker, owner_token, worker_token = service
    base = f"/api/v1/projects/{project}/cord"
    body = {"recipient": worker, "subject": "Review", "body": "Please inspect this change"}
    sent = client.post(base + "/messages", json=body, headers=headers(owner_token, "send"))
    assert sent.status_code == 201, sent.text
    message = sent.json()
    message_id = message["message_id"]
    assert message["sender"] == owner
    spoofed = client.post(base + "/messages", json={**body, "sender": worker}, headers=headers(owner_token, "spoof"))
    assert spoofed.status_code == 422
    receipt = client.post(base + f"/messages/{message_id}/receipt", json={}, headers=headers(worker_token, "receipt"))
    assert receipt.status_code == 200, receipt.text
    pending = client.get(base + "/inbox", headers=headers(worker_token))
    assert len(pending.json()) == 1  # A receipt is not handling.
    reply_body = {"subject": "Reviewed", "body": "Changes requested", "handle_original": True}
    reply = client.post(base + f"/messages/{message_id}/reply", json=reply_body, headers=headers(worker_token, "reply"))
    assert reply.status_code == 200, reply.text
    replay = client.post(base + f"/messages/{message_id}/reply", json=reply_body, headers=headers(worker_token, "reply"))
    assert replay.json() == reply.json()
    assert client.get(base + "/inbox", headers=headers(worker_token)).json() == []
    owner_inbox = client.get(base + "/inbox", headers=headers(owner_token)).json()
    assert len(owner_inbox) == 1
    assert owner_inbox[0]["reply_to"] == message_id


def test_http_readiness_and_persistence_across_app_recreation(service):
    client, store, project, _, _, token, _ = service
    assert client.get("/health/ready").status_code == 200
    base = f"/api/v1/projects/{project}/tasks"
    response = client.post(base, json={"task_id": "persistent", "title": "Keep", "description": "Survive app recreation"}, headers=headers(token, "persist"))
    assert response.status_code == 201, response.text
    with TestClient(create_app(store), raise_server_exceptions=False) as restarted:
        loaded = restarted.get(base + "/persistent", headers=headers(token))
        assert loaded.status_code == 200
        assert loaded.json() == response.json()


def test_http_preserves_literal_unicode_escape_but_rejects_nul(service):
    client, _, project, _, _, token, _ = service
    base = f"/api/v1/projects/{project}/tasks"
    literal = "Code example: " + chr(92) + "u0000"
    created = client.post(base, json={
        "task_id": "escape", "title": "Code text", "description": literal,
        "metadata": {literal: literal},
    }, headers=headers(token, "escape-create"))
    assert created.status_code == 201, created.text
    assert created.json()["description"] == literal
    assert created.json()["metadata"] == {literal: literal}
    changed = client.patch(base + "/escape", json={"description": literal + " updated"}, headers=headers(token, "escape-update", 1))
    assert changed.status_code == 200, changed.text
    assert changed.json()["description"] == literal + " updated"
    refused = client.patch(base + "/escape", json={"metadata": {"bad": "actual\x00nul"}}, headers=headers(token, "nul", 2))
    assert refused.status_code == 422
    current = client.get(base + "/escape", headers=headers(token)).json()
    assert current["revision"] == 2
    assert current["metadata"][literal] == literal
    assert set(current["metadata"]) == {literal, "_skybuild_workflow"}


def test_cli_service_process_restart_preserves_task(service, tmp_path):
    _, _, project, _, _, token, _ = service
    dsn = os.environ["SKYBUILD_HTTP_TEST_DSN"]
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    env = {**os.environ, "SKYBUILD_DSN": dsn,
           "SKYBUILD_EXPECTED_DATABASE": conninfo_to_dict(dsn)["dbname"]}
    base = f"http://127.0.0.1:{port}"
    task_path = f"/api/v1/projects/{project}/tasks"
    created = None
    for attempt in range(2):
        with (tmp_path / f"server-{attempt}.log").open("w") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "skybuild", "serve", "--port", str(port)],
                env=env, stdout=log, stderr=subprocess.STDOUT,
            )
            try:
                deadline = time.monotonic() + 10
                with httpx.Client(base_url=base, timeout=1, trust_env=False) as http:
                    while True:
                        if process.poll() is not None:
                            pytest.fail("Disposable service exited before readiness")
                        try:
                            if http.get("/health/ready").status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        if time.monotonic() >= deadline:
                            pytest.fail("Disposable service did not become ready within ten seconds")
                        time.sleep(0.05)
                    if attempt == 0:
                        response = http.post(task_path, headers=headers(token, "process-create"), json={
                            "task_id": "process-restart", "title": "Restart", "description": "Durable task",
                        })
                        assert response.status_code == 201, response.text
                        created = response.json()
                    else:
                        response = http.get(task_path + "/process-restart", headers=headers(token))
                        assert response.status_code == 200, response.text
                        assert response.json() == created
            finally:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
