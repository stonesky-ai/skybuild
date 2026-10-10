"""Petri transaction and immutable attempt binding checks."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from threading import Barrier
from uuid import uuid4

import pytest

from skybuild.contracts import DomainError
from skybuild.store import Store
from skybuild.workflow import TaskToken, Place, ValidationResult, ValidationStage, ResultState
from test_store import store, actors, create
from test_claims import ready


def enrolled(store, people, project, task_id="petri-task"):
    task = ready(store, people, project, task_id)
    return store.initialize_workflow(people["owner"], project, task_id, task["revision"], "initialize-" + task_id)["task"]


def test_atomic_claim_moves_working_and_preserves_readiness(store, actors):
    project, people = actors
    task = enrolled(store, people, project)
    before_generation = Store.workflow_token(task).input_generation
    claim = store.claim_task(people["worker"], project, task["task_id"], task["revision"], "claim")
    after = store.get_task(people["owner"], project, task["task_id"])
    token = Store.workflow_token(after)
    assert token.place == Place.WORKING
    assert token.revision == claim["task_revision"] == task["revision"] + 1
    assert token.input_generation == before_generation
    assert token.claim_fence == claim["fence"]
    assert store.claim_task(people["worker"], project, task["task_id"], task["revision"], "claim") == claim
    with store._connection() as connection:
        readiness = connection.execute("SELECT * FROM task_readiness WHERE project_id = %s AND task_id = %s", (project, task["task_id"])).fetchone()
    assert readiness["input_generation"] == readiness["assessed_generation"] == before_generation


def test_claim_journal_failure_rolls_back_task_and_claim(store, actors, monkeypatch):
    project, people = actors
    task = enrolled(store, people, project)
    original = store._journal
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("Injected journal failure")
    monkeypatch.setattr(store, "_journal", fail)
    with pytest.raises(RuntimeError):
        store.claim_task(people["worker"], project, task["task_id"], task["revision"], "claim")
    assert store.get_task(people["owner"], project, task["task_id"]) == task
    assert store.claim_history(people["owner"], project, task["task_id"]) == []


def test_two_claimers_commit_one_working_token(store, actors):
    project, people = actors
    task = enrolled(store, people, project)
    barrier = Barrier(2)
    def claim(name):
        barrier.wait()
        try:
            return store.claim_task(people[name], project, task["task_id"], task["revision"], name)
        except DomainError as error:
            return error
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ["worker", "peer"]))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert Store.workflow_token(store.get_task(people["owner"], project, task["task_id"])).place == Place.WORKING
    assert len(store.claim_history(people["owner"], project, task["task_id"])) == 1


def test_unknown_definition_enrolls_hold_without_rewriting_history(store, actors):
    project, people = actors
    task = create(store, people["owner"], project)
    with store._connection() as connection:
        old = connection.execute("SELECT * FROM task_journal WHERE project_id = %s AND task_id = %s ORDER BY revision", (project, task["task_id"])).fetchall()
    view = store.initialize_workflow(people["owner"], project, task["task_id"], task["revision"], "init")
    assert view["token"]["place"] == "hold"
    assert view["token"]["hold_reason"]
    assert view["task"]["status"] == task["status"]
    with store._connection() as connection:
        current = connection.execute("SELECT * FROM task_journal WHERE project_id = %s AND task_id = %s ORDER BY revision", (project, task["task_id"])).fetchall()
    assert current[:len(old)] == old
    assert len(current) == len(old) + 1


def test_caller_cannot_supply_guard_facts_or_producer_acceptance(store, actors):
    project, people = actors
    task = enrolled(store, people, project)
    for body in ({"context": {"admission_permitted": True}}, {"place": "done"}):
        with pytest.raises(DomainError) as caught:
            store.workflow_transition(people["owner"], project, task["task_id"], "submit", body, task["revision"], uuid4().hex)
        assert caught.value.code == "validation"
    with pytest.raises(DomainError):
        store.workflow_transition(people["owner"], project, task["task_id"], "accept", {}, task["revision"], "accept")


def test_reservation_pins_current_cas_and_immutable_claim_revision(store, actors):
    project, people = actors
    task = enrolled(store, people, project)
    claim = store.claim_task(people["worker"], project, task["task_id"], task["revision"], "claim")
    # Simulate trusted progress that changes CAS, not the immutable attempt inputs.
    with store._connection() as connection:
        connection.execute("UPDATE tasks SET revision = revision + 1 WHERE project_id = %s AND task_id = %s", (project, task["task_id"]))
    current = store.get_task(people["owner"], project, task["task_id"])
    token = Store.workflow_token(current)
    store.configure_cpu_pool(people["owner"], project, 1, True, 0, "pool", reason="Test pool")
    store.set_cpu_local_control(people["owner"], project, True, 1, "local", reason="Test controls")
    action_id = uuid4().hex
    explained = store.explain_cpu(people["worker"], project, task["task_id"], action_id, token.attempt_id, 1,
                                  current["revision"], token.input_generation, claim["fence"], 1, 2)
    assert explained['eligible'] is True
    result = store.reserve_cpu(people["worker"], project, task["task_id"], action_id, token.attempt_id, 1,
                               current["revision"], token.input_generation, claim["fence"], 1, 2)
    assert result["task_revision"] == current["revision"]
    assert result["claim_task_revision"] == claim["task_revision"]
    assert result["task_revision"] != result["claim_task_revision"]


def token_task(token):
    return {"project_id": token.project_id, "task_id": token.task_id, "title": "Current title", "priority": 2,
            "dependencies": [], "responsible": "owner", "next_action": "Continue", "blocker": None,
            "revision": 10, "status": "in-progress",
            "metadata": {"_skybuild_workflow": {"petri": {"schema_version": 1, "token": token.to_dict()}}}}


def test_projection_uses_row_identity_and_nested_result_records():
    evidence = ValidationResult("project", "task", ValidationStage.UNIT_TESTS, ResultState.PASSED,
                                source_head="head", artifacts=("artifact/test",))
    task = token_task(TaskToken("project", "task", Place.VALIDATING, source_head="head", evidence=(evidence,)))
    token = Store.workflow_token(task)
    assert token.title == "Current title" and token.revision == 10
    assert token.evidence == (evidence,)
    projection = Store.workflow_projection(task)
    assert projection["evidence_freshness"] == "current"
    assert projection["enabled_actions"] == []
    task["metadata"]["_skybuild_workflow"]["petri"]["token"]["source_head"] = "changed"
    assert Store.workflow_projection(task)["evidence_freshness"] == "stale"


def test_attempt_binding_never_accepts_revision_ordering_shortcut():
    token = TaskToken("project", "task", Place.WORKING, attempt_id="attempt", claim_fence=4, input_generation=7)
    task = token_task(token)
    claim = {"task_revision": 5, "fence": 4}
    petri = task["metadata"]["_skybuild_workflow"]["petri"]
    petri["attempt_binding"] = {"task_revision": 5, "input_generation": 7, "attempt_id": "attempt", "claim_fence": 4}
    store = Store("unused", "unused")
    assert store._cpu_task_binding(task, claim, 7, "attempt")
    for field, value in (("task_revision", 4), ("input_generation", 6), ("attempt_id", "other"), ("claim_fence", 3)):
        changed = deepcopy(task)
        changed["metadata"]["_skybuild_workflow"]["petri"]["attempt_binding"][field] = value
        assert not store._cpu_task_binding(changed, claim, 7, "attempt")
    pending = deepcopy(task)
    pending["metadata"]["_skybuild_workflow"]["petri"]["token"]["pending_action"] = "hold"
    assert not store._cpu_task_binding(pending, claim, 7, "attempt")


def author_receipt(token):
    return {"source_head": "a" * 40, "target_base": "b" * 40, "source_branch": "refs/heads/task/proposal",
            "attempt_id": token.attempt_id, "claim_fence": token.claim_fence,
            "input_generation": token.input_generation, "definition_revision": token.definition_revision,
            "policy_version": token.policy_version}


def test_submission_binds_author_output_without_rewriting_claim_inputs(store, actors):
    project, people = actors
    task = enrolled(store, people, project)
    claim = store.claim_task(people["worker"], project, task["task_id"], task["revision"], "claim")
    working = store.get_task(people["owner"], project, task["task_id"])
    token = Store.workflow_token(working)
    receipt = author_receipt(token)
    view = store.workflow_transition(people["worker"], project, task["task_id"], "submit", receipt,
                                     working["revision"], "submit")
    assert view["token"]["place"] == "validating"
    assert view["token"]["source_head"] == receipt["source_head"]
    assert view["token"]["input_generation"] == token.input_generation + 1
    binding = view["task"]["metadata"]["_skybuild_workflow"]["petri"]["attempt_binding"]
    assert binding["task_revision"] == claim["task_revision"]
    assert binding["input_generation"] == token.input_generation
    assert store.workflow_transition(people["worker"], project, task["task_id"], "submit", receipt,
                                      working["revision"], "submit") == view
    with store._connection() as connection:
        unchanged = connection.execute("SELECT task_revision FROM task_claims WHERE project_id = %s AND task_id = %s",
                                       (project, task["task_id"])).fetchone()
        events = connection.execute("SELECT event_facts FROM task_journal WHERE project_id = %s AND task_id = %s AND operation = 'workflow.submit'",
                                    (project, task["task_id"])).fetchall()
    assert unchanged["task_revision"] == claim["task_revision"]
    assert len(events) == 1 and events[0]["event_facts"]["author_output_receipt"] == receipt
    with pytest.raises(DomainError) as caught:
        store.workflow_transition(people["worker"], project, task["task_id"], "submit",
                                  {**receipt, "source_head": "c" * 40}, working["revision"], "submit")
    assert caught.value.code == "idempotency_conflict"


def test_submission_rejects_stale_former_worker_and_forged_snapshot(store, actors):
    project, people = actors
    task = enrolled(store, people, project)
    store.claim_task(people["worker"], project, task["task_id"], task["revision"], "claim")
    working = store.get_task(people["owner"], project, task["task_id"])
    receipt = author_receipt(Store.workflow_token(working))
    for principal, change in ((people["peer"], {}), (people["worker"], {"claim_fence": 2}),
                              (people["worker"], {"input_generation": receipt["input_generation"] + 1})):
        with pytest.raises(DomainError):
            store.workflow_transition(principal, project, task["task_id"], "submit", {**receipt, **change},
                                      working["revision"], uuid4().hex)
    assert store.get_task(people["owner"], project, task["task_id"]) == working


def test_verified_adapter_rejects_nonadmin_and_input_fact_replacement(store, actors):
    project, people = actors
    task = enrolled(store, people, project)
    for principal, facts in ((people["worker"], {}), (people["owner"], {"effects_resolved": True})):
        with pytest.raises(DomainError):
            store.verified_workflow_transition(principal, project, task["task_id"], "freeze", {}, task["revision"],
                                               uuid4().hex, evidence={"receipt": "ref"},
                                               verifier=lambda *args: facts)
    assert store.get_task(people["owner"], project, task["task_id"]) == task


def test_full_failed_result_survives_compact_token_journal(store, actors):
    from skybuild.workflow import TRANSITIONS
    if not any(spec.event == "validation_result" for spec in TRANSITIONS):
        pytest.skip("Run on the composed Task03/04 candidate")
    project, people = actors
    task = enrolled(store, people, project)
    store.claim_task(people["worker"], project, task["task_id"], task["revision"], "claim")
    working = store.get_task(people["owner"], project, task["task_id"])
    submitted = store.workflow_transition(people["worker"], project, task["task_id"], "submit",
        author_receipt(Store.workflow_token(working)), working["revision"], "submit")
    token = Store.workflow_token(submitted["task"])
    result = ValidationResult(project, task["task_id"], ValidationStage.UNIT_TESTS, ResultState.FAILED,
        attempt_id=token.attempt_id, source_head=token.source_head, target_base=token.target_base,
        input_generation=token.input_generation, definition_revision=token.definition_revision,
        policy_version=token.policy_version, claim_fence=token.claim_fence, producer=people["worker"].principal_id,
        check_id="unit", findings=("Fault " + "x" * 3500,), artifacts=("artifact/failure",))
    body = {"result": result.to_dict()}
    failed = store.workflow_transition(people["worker"], project, task["task_id"], "validation_result", body,
                                       token.revision, "failed-result")
    assert failed["token"]["place"] == "ready"
    with store._connection() as connection:
        event = connection.execute("SELECT event_facts FROM task_journal WHERE project_id = %s AND task_id = %s AND operation = 'workflow.validation_result'",
                                   (project, task["task_id"])).fetchone()
    assert event["event_facts"]["result"] == result.to_dict()
    assert store.workflow_transition(people["worker"], project, task["task_id"], "validation_result", body,
                                      token.revision, "failed-result") == failed
