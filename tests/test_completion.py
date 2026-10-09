"""Manual acceptance evidence must be exact, independent and retained."""

from copy import deepcopy
import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict

from skybuild.api import create_app
from skybuild.completion import completion_change, current_completion
from skybuild.contracts import DomainError
from skybuild.store import Store
from test_store import seed_api_authority


def evidence():
    head = "a" * 40
    return {"reason": "Accepted exact tested publication", "generation": 0, "source_head": head,
            "author": "author-session", "policy_ref": "accepted-policy@1",
            "acceptance": [{"criterion": "Pass meaningful checks", "evidence_ref": "checks/log@1"}],
            "checks": [{"name": "pytest", "source_head": head, "result": "passed", "evidence_ref": "checks/log@1"}],
            "review": {"reviewer": "independent-reviewer", "session_ref": "review/session@1", "source_head": head,
                       "result": "passed", "unresolved_blocking_findings": 0, "evidence_ref": "review/report@1"},
            "publication": {"source_head": head, "candidate_commit": "b" * 40, "base_commit": "c" * 40,
                            "target_commit": "d" * 40, "target_ref": "refs/heads/dev-001", "result": "confirmed",
                            "inclusion_evidence_ref": "publication/inclusion@1"}}


def task():
    return {"revision": 1, "title": "Task", "description": "Exact definition", "dependencies": [],
            "acceptance_criteria": ["Pass meaningful checks"], "architecture_refs": [],
            "metadata": {}, "status": "proposed"}


@pytest.mark.parametrize("change,code", [
    (lambda body: body.update(generation=1), "stale_evidence"),
    (lambda body: body["acceptance"][0].update(criterion="Old definition"), "stale_evidence"),
    (lambda body: body.update(acceptance=[]), "validation"),
    (lambda body: body["checks"][0].update(result="failed"), "stale_evidence"),
    (lambda body: body["checks"][0].update(source_head="e" * 40), "stale_evidence"),
    (lambda body: body["review"].update(reviewer=body["author"]), "workflow_conflict"),
    (lambda body: body["review"].update(unresolved_blocking_findings=1), "workflow_conflict"),
    (lambda body: body["review"].update(unresolved_blocking_findings=False), "workflow_conflict"),
    (lambda body: body["publication"].update(result="pending"), "workflow_conflict"),
    (lambda body: body["publication"].update(source_head="e" * 40), "workflow_conflict"),
    (lambda body: body["publication"].update(target_commit="short"), "validation"),
    (lambda body: body.update(override=True), "validation"),
])
def test_completion_rejects_missing_stale_or_inconsistent_evidence(change, code):
    body = evidence()
    change(body)
    with pytest.raises(DomainError) as error:
        completion_change(task(), body, "owner")
    assert error.value.code == code


def test_completion_snapshot_becomes_stale_on_changed_inputs():
    before, body = task(), evidence()
    after = {**before, **completion_change(before, body, "owner")}
    assert current_completion(after)
    assert after["metadata"]["_skybuild_completion"]["kind"] == "owner_attestation"
    body["checks"][0]["result"] = "failed"
    assert after["metadata"]["_skybuild_completion"]["checks"][0]["result"] == "passed"
    after["description"] = "Changed definition"
    assert not current_completion(after)
    after["description"] = before["description"]
    after["metadata"]["_skybuild_workflow"] = {"generation": 1}
    assert not current_completion(after)


@pytest.fixture
def service():
    dsn = os.environ.get("SKYBUILD_TEST_DSN")
    if not dsn:
        pytest.skip("Set SKYBUILD_TEST_DSN to a task-owned disposable database")
    database = conninfo_to_dict(dsn).get("dbname", "")
    if not database.startswith("skybuild_test"):
        pytest.fail("Completion tests require an explicitly named disposable skybuild_test database")
    store = Store(dsn, database)
    store.migrate()
    project = "completion-" + uuid4().hex
    seed_api_authority(store, project)
    tokens = {name: uuid4().hex * 2 for name in ("owner", "worker")}
    for name, token in tokens.items():
        store.provision_principal(name + "-" + project, token, is_admin=name == "owner",
                                  grants={project: ["tasks:read", "tasks:write"]})
    with TestClient(create_app(store)) as client:
        yield client, store, project, tokens


def headers(token, key, revision=None):
    result = {"Authorization": "Bearer " + token, "Idempotency-Key": key}
    if revision is not None:
        result["If-Match"] = str(revision)
    return result


def test_completion_http_atomic_replay_authorization_and_reopening(service):
    client, store, project, tokens = service
    base = f"/api/v1/projects/{project}/tasks"
    create = {"task_id": "T1", "title": "Task", "description": "Exact definition",
              "acceptance_criteria": ["Pass meaningful checks"]}
    assert client.post(base, json=create, headers=headers(tokens["owner"], "create")).status_code == 201
    body = evidence()
    assert client.post(base + "/T1/complete", json=body, headers=headers(tokens["worker"], "worker", 1)).status_code == 403
    missing = deepcopy(body)
    missing["acceptance"] = []
    assert client.post(base + "/T1/complete", json=missing, headers=headers(tokens["owner"], "invalid", 1)).status_code == 422
    complete = client.post(base + "/T1/complete", json=body, headers=headers(tokens["owner"], "complete", 1))
    assert complete.status_code == 200, complete.text
    done = complete.json()
    assert done["revision"] == 2 and current_completion(done)
    assert client.post(base + "/T1/complete", json=body, headers=headers(tokens["owner"], "complete", 1)).json() == done
    assert client.post(base + "/T1/complete", json=body, headers=headers(tokens["owner"], "stale", 1)).status_code == 409
    for key in ("_skybuild_workflow", "_skybuild_completion"):
        assert client.patch(base + "/T1", json={"metadata": {key: {}}},
                            headers=headers(tokens["owner"], "inject-" + key, 2)).status_code == 422
    reopened = client.post(base + "/T1/actions/rework", json={"reason": "Changed acceptance"},
                           headers=headers(tokens["owner"], "reopen", 2))
    assert reopened.status_code == 200
    assert not current_completion(reopened.json())
    history = client.get(base + "/T1/history", headers=headers(tokens["owner"], "read")).json()
    assert [row["operation"] for row in history] == ["created", "completed", "rework"]
    assert current_completion(history[1]["after_state"])
    assert history[1]["after_state"]["metadata"]["_skybuild_completion"]["actor"] == "owner-" + project


def test_completion_requires_current_dependency_evidence(service):
    client, store, project, tokens = service
    owner = store.authenticate(tokens["owner"])
    store.create_task(owner, project, {"task_id": "D", "title": "Dependency", "description": "Definition"}, "dependency")
    store.create_task(owner, project, {"task_id": "T", "title": "Task", "description": "Definition",
                                      "acceptance_criteria": ["Pass meaningful checks"], "dependencies": ["D"]}, "task")
    with pytest.raises(DomainError) as error:
        store.complete_task(owner, project, "T", evidence(), 1, "complete")
    assert error.value.code == "workflow_conflict"
    assert store.get_task(owner, project, "T")["revision"] == 1


def test_edit_and_revert_cannot_revive_previous_completion(service):
    client, store, project, tokens = service
    owner = store.authenticate(tokens["owner"])
    initial = store.create_task(owner, project, {"task_id": "T", "title": "Task", "description": "Original",
                                               "acceptance_criteria": ["Pass meaningful checks"]}, "create")
    completed = store.complete_task(owner, project, "T", evidence(), initial["revision"], "complete")
    assert current_completion(completed)
    changed = store.update_task(owner, project, "T", {"description": "Changed"}, completed["revision"], "edit")
    reverted = store.update_task(owner, project, "T", {"description": "Original"}, changed["revision"], "revert")
    assert reverted["status"] == "blocked"
    assert reverted["metadata"]["_skybuild_workflow"]["generation"] == 2
    assert not current_completion(reverted)
    with pytest.raises(DomainError) as error:
        store.complete_task(owner, project, "T", evidence(), reverted["revision"], "reuse-evidence")
    assert error.value.code == "stale_evidence"
    history = store.task_history(owner, project, "T")
    assert current_completion(history[1]["after_state"])


def test_dependency_bearing_completion_accepts_current_attested_dependency(service):
    client, store, project, tokens = service
    owner = store.authenticate(tokens["owner"])
    store.create_task(owner, project, {"task_id": "D", "title": "Dependency", "description": "Definition",
                                      "acceptance_criteria": ["Pass meaningful checks"]}, "dependency")
    store.complete_task(owner, project, "D", evidence(), 1, "complete-dependency")
    store.create_task(owner, project, {"task_id": "T", "title": "Task", "description": "Definition",
                                      "acceptance_criteria": ["Pass meaningful checks"], "dependencies": ["D"]}, "task")
    completed = store.complete_task(owner, project, "T", evidence(), 1, "complete")
    assert current_completion(completed)
    assert completed["revision"] == 2
