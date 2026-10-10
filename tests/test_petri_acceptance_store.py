"""Final task acceptance on an explicitly selected disposable PostgreSQL database."""
from copy import deepcopy
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

from skybuild.contracts import DomainError
from skybuild.enrollment import DEFAULT_POLICY_VERSION, DEFAULT_REQUIREMENTS
from skybuild.store import Store
from skybuild.workflow import Place
from test_store import actors, seed_api_authority, store


def create(store, principal, project, task_id, **fields):
    """Exercise the production default creation path without a legacy fixture."""
    return store.create_task(principal, project, {"task_id": task_id, "title": "Work",
        "description": "Full task scope", **fields}, "create-" + task_id)
from test_petri_store import author_receipt



def completed_petri_fixture(store, people, project, task_id, **fields):
    """Establish current Petri acceptance through guarded production interfaces."""
    from skybuild.completion import current_completion, generation
    from skybuild.integration_workflow import IntegrationWorkflow
    from skybuild.workflow import ResultState, ValidationResult, ValidationStage
    owner, worker = people["owner"], people["worker"]
    task = create(store, owner, project, task_id, acceptance_criteria=["Verify result"], **fields)
    claim = store.claim_task(worker, project, task_id, task["revision"], task_id + "-claim")
    task = store.get_task(owner, project, task_id)
    token = Store.workflow_token(task)
    view = store.workflow_transition(worker, project, task_id, "submit", author_receipt(token), token.revision, task_id + "-submit")
    for stage in ValidationStage:
        token = Store.workflow_token(view["task"])
        principal = owner if stage == ValidationStage.CODE_REVIEW else worker
        evidence = ValidationResult(project, task_id, stage, ResultState.PASSED,
            source_head=token.source_head, target_base=token.target_base, attempt_id=token.attempt_id,
            claim_fence=token.claim_fence, input_generation=token.input_generation,
            definition_revision=token.definition_revision, policy_version=token.policy_version,
            producer=principal.principal_id, check_id=stage.value, tool_version="acceptance-v1")
        view = store.workflow_transition(principal, project, task_id, "validation_result", {"result": evidence.to_dict()},
                                         token.revision, task_id + "-" + stage.value)
    store.release_claim(worker, project, task_id, claim["fence"], view["task"]["revision"], task_id + "-release", reason="Checks complete")
    token = Store.workflow_token(view["task"])
    policy = {"version": token.policy_version, "publication_required": False, "reason": "Isolated test acceptance",
        "requirements": [stage.value for stage in token.requirements], "review_required": True}
    common = {"publication_required": False, "policy_reason": policy["reason"],
        "publication_policy_version": token.policy_version, "acceptance_policy": policy}
    adapter = IntegrationWorkflow(store, lambda *args: {**common, "integration_fixed": True})
    frozen = adapter.transition(owner, project, task_id, "freeze", evidence={"policy_ref": token.policy_version},
                                expected_revision=token.revision, idempotency_key=task_id + "-freeze")
    completion = {"kind": "without_publication", "reason": "Verified test acceptance", "generation": generation(frozen["task"]),
        "author": worker.principal_id, "policy_ref": token.policy_version,
        "acceptance": [{"criterion": item, "evidence_ref": "acceptance/" + task_id} for item in frozen["task"]["acceptance_criteria"]]}
    adapter = IntegrationWorkflow(store, lambda *args: {**common, "acceptance_verified": True, "completion_evidence": completion})
    accepted = adapter.transition(owner, project, task_id, "accept", evidence={"acceptance_ref": "acceptance/" + task_id},
                                  expected_revision=frozen["task"]["revision"], idempotency_key=task_id + "-accept")
    assert current_completion(accepted["task"])
    return accepted["task"]

def test_new_task_in_any_project_can_claim_and_submit_without_profile_patch(store, actors):
    project, people = actors
    second = "example-" + uuid4().hex
    seed_api_authority(store, second)
    for selected in (project, second):
        task = create(store, people["owner"], selected, "same-task", acceptance_criteria=["Verify work"])
        token = Store.workflow_token(task)
        assert token.place == Place.READY
        assert token.policy_version == DEFAULT_POLICY_VERSION
        assert token.requirements == DEFAULT_REQUIREMENTS
        task = store.task_action(people["owner"], selected, "same-task", "ready", {"reason": "Reviewed"},
                                 task["revision"], "ready-same")
        claim = store.claim_task(people["owner"], selected, "same-task", task["revision"], "claim-same")
        current = store.get_task(people["owner"], selected, "same-task")
        token = Store.workflow_token(current)
        submitted = store.workflow_transition(people["owner"], selected, "same-task", "submit", author_receipt(token),
                                              token.revision, "submit-same")
        assert submitted["token"]["place"] == "validating"
        assert submitted["token"]["claim_fence"] == claim["fence"]
    with pytest.raises(DomainError) as error:
        store.get_task(people["worker"], second, "same-task")
    assert error.value.status_code == 403


@pytest.mark.parametrize("status,phase", [("in-progress", "ready-for-review"), ("done", "done"),
                                         ("blocked", "unknown"), ("deferred", "deferred")])
def test_snapshot_migration_rehearsal_preserves_journal_and_holds_ambiguous_work(store, actors, status, phase):
    """Construct a captured legacy row only in the disposable test database."""
    project, people = actors
    task = create(store, people["owner"], project, "legacy-snapshot")
    metadata = deepcopy(task["metadata"])
    metadata["_skybuild_workflow"].pop("petri")
    with store._connection() as connection:
        connection.execute("UPDATE tasks SET status = %s, phase = %s, metadata = %s WHERE project_id = %s AND task_id = %s",
                           (status, phase, Jsonb(metadata), project, task["task_id"]))
    before = store.get_task(people["owner"], project, task["task_id"])
    history = store.task_history(people["owner"], project, task["task_id"])
    view = store.initialize_workflow(people["owner"], project, task["task_id"], before["revision"], "migrate-snapshot")
    assert view["token"]["place"] == "hold"
    assert view["token"]["hold_reason"]
    assert view["task"]["task_id"] == before["task_id"]
    assert view["task"]["status"] == before["status"]
    after_history = store.task_history(people["owner"], project, task["task_id"])
    assert after_history[:-1] == history
    assert after_history[-1]["operation"] == "workflow_initialized"
    assert store.initialize_workflow(people["owner"], project, task["task_id"], before["revision"], "migrate-snapshot") == view



def test_verified_progress_journals_receipt_and_exclusion_replays_once(store, actors):
    from dataclasses import replace
    from skybuild.integration_workflow import IntegrationWorkflow
    from skybuild.workflow import ResultState, ValidationResult, ValidationStage
    project, people = actors
    task = create(store, people["owner"], project, "integration-observation", acceptance_criteria=["Verify"])
    original = Store.workflow_token(task)
    evidence = ValidationResult(project, task["task_id"], ValidationStage.CODE_REVIEW, ResultState.PASSED,
        source_head="a" * 40, target_base="b" * 40, input_generation=original.input_generation,
        definition_revision=original.definition_revision, policy_version=original.policy_version,
        producer="independent-reviewer", check_id="review")
    token = replace(original, place=Place.INTEGRATING, source_head="a" * 40, target_base="b" * 40,
                    bundle_id="bundle-1", requirements=(ValidationStage.CODE_REVIEW,), evidence=(evidence,))
    metadata = deepcopy(task["metadata"])
    metadata["_skybuild_workflow"]["petri"]["token"] = token.to_dict()
    with store._connection() as connection:
        connection.execute("UPDATE tasks SET metadata = %s, status = 'in-progress', phase = 'integrating' WHERE project_id = %s AND task_id = %s",
                           (Jsonb(metadata), project, task["task_id"]))
        connection.execute("UPDATE task_readiness SET assessed_generation = input_generation WHERE project_id = %s AND task_id = %s",
                           (project, task["task_id"]))
    adapter = IntegrationWorkflow(store, lambda *args: {
        "integration_observation_verified": True, "bundle_id": "bundle-1", "publication_outcome": "unknown"})
    receipt = {"outcome": "unknown", "operation_ref": "publication-1"}
    view = adapter.transition(people["owner"], project, task["task_id"], "integration_progress",
        reason="Lost response", evidence=receipt, expected_revision=task["revision"], idempotency_key="observe")
    assert view["token"]["place"] == "integrating"
    journal = store.task_history(people["owner"], project, task["task_id"])
    assert journal[-1]["event_facts"]["integration_receipt"] == receipt
    assert adapter.transition(people["owner"], project, task["task_id"], "integration_progress",
        reason="Lost response", evidence=receipt, expected_revision=task["revision"], idempotency_key="observe") == view
    # A deployment verifier must establish that publication was never dispatched.
    adapter = IntegrationWorkflow(store, lambda *args: {"exclusion_verified": True,
        "validation_verified": True, "bundle_id": "bundle-1", "publication_outcome": "unpublished"})
    excluded = adapter.transition(people["owner"], project, task["task_id"], "exclude_from_bundle",
        reason="Confirmed unpublished", evidence={"outcome": "unpublished"},
        expected_revision=view["task"]["revision"], idempotency_key="exclude")
    assert excluded["token"]["place"] == "validating" and excluded["token"]["bundle_id"] is None
    assert excluded["token"]["evidence"] == view["token"]["evidence"]



def test_api_default_creation_reaches_verified_acceptance_with_all_five_stages(store, actors):
    from fastapi.testclient import TestClient
    from skybuild.api import create_app
    from skybuild.completion import current_completion, generation
    from skybuild.integration_workflow import IntegrationWorkflow
    from skybuild.workflow import ResultState, ValidationResult, ValidationStage
    project, people = actors
    root = f"/api/v1/projects/{project}/tasks"
    with TestClient(create_app(store)) as client:
        client.headers["Authorization"] = "Bearer " + people["owner_token"]
        response = client.post(root, json={"task_id": "default-end-to-end", "title": "Build feature",
            "description": "Implement and verify a bounded feature", "acceptance_criteria": ["Verify feature"]},
            headers={"Idempotency-Key": "create-default-end-to-end"})
        assert response.status_code in {200, 201}
        task = response.json()
        path = root + "/" + task["task_id"]
        client.headers["Authorization"] = "Bearer " + people["worker_token"]
        view = client.get(path + "/workflow").json()
        assert view["token"]["place"] == "ready"
        assert view["token"]["policy_version"] == DEFAULT_POLICY_VERSION
        assert "claim" in view["available_actions"]
        response = client.post(path + "/claim", json={"lease_seconds": 300},
            headers={"If-Match": str(task["revision"]), "Idempotency-Key": "default-claim"})
        assert response.status_code == 200
        view = client.get(path + "/workflow").json()
        token = Store.workflow_token(view["task"])
        assert token.place == Place.WORKING and token.revision == response.json()["task_revision"]
        response = client.post(path + "/workflow", json={"event": "submit", **author_receipt(token)},
            headers={"If-Match": str(token.revision), "Idempotency-Key": "default-submit"})
        assert response.status_code == 200
        view = response.json()
        for stage in ValidationStage:
            token = Store.workflow_token(view["task"])
            producer = "owner" if stage == ValidationStage.CODE_REVIEW else "worker"
            client.headers["Authorization"] = "Bearer " + people[producer + "_token"]
            result = ValidationResult(project, task["task_id"], stage, ResultState.PASSED,
                source_head=token.source_head, target_base=token.target_base, attempt_id=token.attempt_id,
                claim_fence=token.claim_fence, definition_revision=token.definition_revision,
                input_generation=token.input_generation, policy_version=token.policy_version,
                producer=people[producer].principal_id, check_id=stage.value, tool_version="acceptance-v1")
            response = client.post(path + "/workflow", json={"event": "validation_result", "result": result.to_dict()},
                headers={"If-Match": str(token.revision), "Idempotency-Key": "default-" + stage.value})
            assert response.status_code == 200
            view = response.json()
        token = Store.workflow_token(view["task"])
        assert len(token.evidence) == 5
        store.release_claim(people["worker"], project, task["task_id"], token.claim_fence, token.revision, "default-release", reason="Checks complete")
        policy = {"version": token.policy_version, "publication_required": False,
            "reason": "Disposable acceptance fixture requires no publication", "requirements": [stage.value for stage in token.requirements],
            "review_required": True}
        common = {"publication_required": False, "policy_reason": policy["reason"],
            "publication_policy_version": token.policy_version, "acceptance_policy": policy}
        adapter = IntegrationWorkflow(store, lambda *args: {**common, "integration_fixed": True})
        frozen = adapter.transition(people["owner"], project, task["task_id"], "freeze", evidence={"policy_ref": token.policy_version},
            expected_revision=token.revision, idempotency_key="default-freeze")
        assert frozen["token"]["place"] == "integrating"
        completion = {"kind": "without_publication", "reason": "Verified isolated fixture", "generation": generation(frozen["task"]),
            "author": people["worker"].principal_id, "policy_ref": token.policy_version,
            "acceptance": [{"criterion": "Verify feature", "evidence_ref": "acceptance/default-end-to-end"}]}
        adapter = IntegrationWorkflow(store, lambda *args: {**common, "acceptance_verified": True, "completion_evidence": completion})
        accepted = adapter.transition(people["owner"], project, task["task_id"], "accept", evidence={"acceptance_ref": "acceptance/default-end-to-end"},
            expected_revision=frozen["task"]["revision"], idempotency_key="default-accept")
        assert accepted["token"]["place"] == "done"
        assert current_completion(accepted["task"])


def test_incomplete_default_create_requires_definition_and_explicit_release(store, actors):
    project, people = actors
    task = create(store, people["owner"], project, "incomplete-default")
    assert Store.workflow_token(task).place == Place.HOLD
    from fastapi.testclient import TestClient
    from skybuild.api import create_app
    path = f"/api/v1/projects/{project}/tasks/{task['task_id']}/workflow"
    with TestClient(create_app(store)) as client:
        client.headers["Authorization"] = "Bearer " + people["owner_token"]
        view = client.get(path).json()
        assert "release_hold" not in view["available_actions"]
        response = client.post(path, json={"event": "release_hold", "reason": "Incomplete release"},
            headers={"If-Match": str(task["revision"]), "Idempotency-Key": "incomplete-release"})
        assert response.status_code == 409
    assert store.get_task(people["owner"], project, task["task_id"]) == task
    changed = store.update_task(people["owner"], project, task["task_id"], {"acceptance_criteria": ["Check definition"]},
                                task["revision"], "define-default")
    assert Store.workflow_token(changed).place == Place.HOLD
    released = store.workflow_transition(people["owner"], project, task["task_id"], "release_hold", {"reason": "Definition checked"},
                                         changed["revision"], "release-default")
    assert released["token"]["place"] == "ready"
    claim = store.claim_task(people["worker"], project, task["task_id"], released["task"]["revision"], "claim-defined-default")
    assert claim["held"]
    assert Store.workflow_token(store.get_task(people["owner"], project, task["task_id"])).place == Place.WORKING



def test_actual_legacy_acceptance_migrates_to_diagnostic_hold_without_new_authority(store, actors):
    from test_dependency_readiness import completed_fixture
    from skybuild.completion import current_completion
    project, people = actors
    accepted = completed_fixture(store, people["owner"], project, "accepted-legacy")
    assert current_completion(accepted)
    history = store.task_history(people["owner"], project, accepted["task_id"])
    view = store.initialize_workflow(people["owner"], project, accepted["task_id"], accepted["revision"], "accepted-legacy-migration")
    assert view["token"]["place"] == "hold"
    assert "Legacy acceptance" in view["token"]["hold_reason"]
    assert not current_completion(view["task"])
    assert view["task"]["metadata"]["_skybuild_completion"] == accepted["metadata"]["_skybuild_completion"]
    assert store.task_history(people["owner"], project, accepted["task_id"])[:-1] == history
