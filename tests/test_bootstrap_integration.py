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
    assert current["metadata"] == {literal: literal}


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
