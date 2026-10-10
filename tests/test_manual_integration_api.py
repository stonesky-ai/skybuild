"""Real PostgreSQL HTTP transitions for the bounded manual owner bridge."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier

from fastapi.testclient import TestClient
import pytest

from skybuild.api import create_app
from skybuild.completion import current_completion
from skybuild.contracts import DomainError
from skybuild.manual_integration import digest, transition
from skybuild.store import Store
from skybuild.workflow import ValidationResult, ValidationStage, ResultState
from test_store import actors, store
from test_petri_store import author_receipt
from test_manual_integration import freeze_evidence, accept_evidence


def validating(store, people, project, task_id="manual-integration", *, checks=True):
    owner, worker = people["owner"], people["worker"]
    task = store.create_task(owner, project, {"task_id": task_id, "title": "Bounded publication",
        "description": "Verify the exact submitted head and publish through a frozen bundle",
        "acceptance_criteria": ["Exact task head reaches the verified target"]}, "create-" + task_id)
    claim = store.claim_task(worker, project, task_id, task["revision"], "claim-" + task_id, lease_seconds=300)
    working = store.get_task(worker, project, task_id)
    token = Store.workflow_token(working)
    view = store.workflow_transition(worker, project, task_id, "submit", author_receipt(token), token.revision, "submit-" + task_id)
    if checks:
        for stage in ValidationStage:
            token = Store.workflow_token(view["task"])
            result = ValidationResult(project, task_id, stage, ResultState.PASSED,
                source_head=token.source_head, target_base=token.target_base, attempt_id=token.attempt_id,
                claim_fence=token.claim_fence, input_generation=token.input_generation,
                definition_revision=token.definition_revision, policy_version=token.policy_version,
                producer=owner.principal_id, check_id=stage.value, tool_version="manual-bridge-test-v1")
            view = store.workflow_transition(owner, project, task_id, "validation_result", {"result": result.to_dict()},
                                             token.revision, task_id + "-" + stage.value)
    store.release_claim(worker, project, task_id, claim["fence"], view["task"]["revision"], "release-" + task_id,
                        reason="Author finished; independent validation and publication remain")
    return view


def post(client, path, packet):
    return client.post(path, json={"event": packet["event"], "evidence": packet},
        headers={"If-Match": str(packet["expected_revision"]), "Idempotency-Key": packet["operation_id"]})


def test_http_freeze_unknown_accept_replay_and_receipt_journal(store, actors):
    project, people = actors
    view = validating(store, people, project)
    frozen_packet = freeze_evidence(Store.workflow_token(view["task"]), people["owner"].principal_id)
    path = f"/api/v1/projects/{project}/tasks/manual-integration/manual-integration"
    with TestClient(create_app(store)) as client:
        client.headers["Authorization"] = "Bearer " + people["owner_token"]
        frozen_response = post(client, path, frozen_packet)
        assert frozen_response.status_code == 200, frozen_response.json()
        frozen = frozen_response.json()
        assert frozen["token"]["place"] == "integrating"
        receipt = store.task_history(people["owner"], project, "manual-integration")[-1]["event_facts"]["integration_receipt"]
        assert receipt == {"authority": "manual_owner_attestation", "sha256": digest(frozen_packet), "evidence": frozen_packet}
        assert post(client, path, frozen_packet).json() == frozen
        unknown = deepcopy(frozen_packet)
        unknown.update(event="integration_progress", operation_id="publication-unknown",
                       expected_revision=frozen["task"]["revision"], freeze_sha256=digest(frozen_packet),
                       publication={"outcome": "unknown", "operation_ref": "publish-original"})
        unknown_response = post(client, path, unknown)
        assert unknown_response.status_code == 200, unknown_response.json()
        assert unknown_response.json()["token"]["place"] == "integrating"
        assert not current_completion(unknown_response.json()["task"])
        accepted_packet = accept_evidence(unknown_response.json(), frozen_packet, people["owner"].principal_id)
        accepted_response = post(client, path, accepted_packet)
        assert accepted_response.status_code == 200, accepted_response.json()
        accepted = accepted_response.json()
        assert accepted["token"]["place"] == "done" and current_completion(accepted["task"])
        assert post(client, path, accepted_packet).json() == accepted
        assert post(client, path, frozen_packet).json() == frozen
        history = store.task_history(people["owner"], project, "manual-integration")
        assert [event["operation"] for event in history[-3:]] == ["workflow.freeze", "workflow.integration_progress", "workflow.accept"]
        assert history[-1]["event_facts"]["integration_receipt"]["sha256"] == digest(accepted_packet)


@pytest.mark.parametrize("change", ["attempt", "generation", "validation", "project", "task", "raw-facts"])
def test_rejected_manual_freeze_changes_neither_task_nor_history(store, actors, change):
    project, people = actors
    view = validating(store, people, project)
    packet = freeze_evidence(Store.workflow_token(view["task"]), people["owner"].principal_id)
    if change == "attempt":
        packet["binding"]["attempt_id"] = "other-attempt"
    elif change == "generation":
        packet["binding"]["input_generation"] += 1
    elif change == "validation":
        packet["validation_sha256"] = "0" * 64
    elif change == "project":
        packet["binding"]["project_id"] = "other-project"
    elif change == "task":
        packet["binding"]["task_id"] = "other-task"
        packet["bundle"]["members"][0]["task_id"] = "other-task"
    else:
        packet["publication_verified"] = True
    before = store.get_task(people["owner"], project, "manual-integration")
    history = store.task_history(people["owner"], project, "manual-integration")
    with TestClient(create_app(store)) as client:
        client.headers["Authorization"] = "Bearer " + people["owner_token"]
        response = post(client, f"/api/v1/projects/{project}/tasks/manual-integration/manual-integration", packet)
        assert response.status_code in {409, 422}, response.json()
    assert store.get_task(people["owner"], project, "manual-integration") == before
    assert store.task_history(people["owner"], project, "manual-integration") == history


def test_worker_and_unqualified_public_paths_cannot_attest(store, actors):
    project, people = actors
    view = validating(store, people, project)
    packet = freeze_evidence(Store.workflow_token(view["task"]), people["owner"].principal_id)
    path = f"/api/v1/projects/{project}/tasks/manual-integration"
    with TestClient(create_app(store)) as client:
        client.headers["Authorization"] = "Bearer " + people["worker_token"]
        assert post(client, path + "/manual-integration", packet).status_code == 403
        client.headers["Authorization"] = "Bearer " + people["owner_token"]
        for event in ("freeze", "accept"):
            assert client.post(path + "/workflow", json={"event": event},
                headers={"If-Match": str(view["task"]["revision"]), "Idempotency-Key": "public-" + event}).status_code == 409
        assert client.post(path + "/manual-integration", json={"event": "exclude_from_bundle", "evidence": packet},
            headers={"If-Match": str(view["task"]["revision"]), "Idempotency-Key": "unsupported"}).status_code == 422


def test_missing_validation_and_changed_frozen_candidate_fail_closed(store, actors):
    project, people = actors
    incomplete = validating(store, people, project, "unchecked", checks=False)
    packet = freeze_evidence(Store.workflow_token(incomplete["task"]), people["owner"].principal_id)
    with pytest.raises(DomainError):
        transition(store, people["owner"], project, "unchecked", "freeze", packet, packet["expected_revision"], packet["operation_id"])
    view = validating(store, people, project)
    packet = freeze_evidence(Store.workflow_token(view["task"]), people["owner"].principal_id)
    frozen = transition(store, people["owner"], project, "manual-integration", "freeze", packet, packet["expected_revision"], packet["operation_id"])
    bad = accept_evidence(frozen, packet, people["owner"].principal_id)
    bad["bundle"]["candidate_commit"] = bad["publication"]["head_commit"] = "9" * 40
    with pytest.raises(DomainError, match="frozen bundle"):
        transition(store, people["owner"], project, "manual-integration", "accept", bad, bad["expected_revision"], bad["operation_id"])
    assert store.task_workflow(people["owner"], project, "manual-integration") == frozen


def test_authorization_is_rechecked_and_failed_journal_rolls_back(store, actors, monkeypatch):
    project, people = actors
    view = validating(store, people, project)
    owner = people["owner"]
    packet = freeze_evidence(Store.workflow_token(view["task"]), owner.principal_id)
    original = store._journal
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("Injected journal failure")
    monkeypatch.setattr(store, "_journal", fail)
    with pytest.raises(RuntimeError):
        transition(store, owner, project, "manual-integration", "freeze", packet, packet["expected_revision"], packet["operation_id"])
    monkeypatch.setattr(store, "_journal", original)
    assert store.task_workflow(owner, project, "manual-integration") == view
    store.provision_principal(owner.principal_id, people["owner_token"], grants={})
    with pytest.raises(DomainError) as error:
        transition(store, owner, project, "manual-integration", "freeze", packet, packet["expected_revision"], packet["operation_id"])
    assert error.value.status_code == 403


def test_racing_freezes_commit_only_one_receipt(store, actors):
    project, people = actors
    view = validating(store, people, project)
    barrier = Barrier(2)
    def freeze(index):
        packet = freeze_evidence(Store.workflow_token(view["task"]), people["owner"].principal_id, "freeze-" + str(index))
        barrier.wait()
        try:
            return transition(store, people["owner"], project, "manual-integration", "freeze", packet, packet["expected_revision"], packet["operation_id"])
        except DomainError as error:
            return error
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(freeze, (1, 2)))
    assert sum(isinstance(outcome, dict) for outcome in outcomes) == 1
    history = store.task_history(people["owner"], project, "manual-integration")
    assert sum(event["operation"] == "workflow.freeze" for event in history) == 1
