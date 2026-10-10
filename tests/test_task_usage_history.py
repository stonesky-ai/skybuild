"""Trusted task-scoped usage history and lineage behavior."""

import secrets
from uuid import uuid4

import pytest

from skybuild.contracts import DomainError
from skybuild.task_usage import _canonical
from test_store import actors, create, store


def _principal(store, project, name, operations):
    principal_id, token = name + "-" + uuid4().hex, secrets.token_urlsafe(32)
    store.provision_principal(principal_id, token, grants={project: set(operations)})
    return store.authenticate(token)


def _usage(task, *, kind="consumed", operation="provider-request-1", quantity="0.75"):
    petri = task["metadata"]["_skybuild_workflow"]["petri"]
    binding = petri["attempt_binding"]
    return {
        "event_kind": kind,
        "attempt_id": binding["attempt_id"],
        "task_revision": binding["task_revision"],
        "definition_revision": petri["token"]["definition_revision"],
        "input_generation": binding["input_generation"],
        "claim_fence": binding["claim_fence"],
        "provider": "provider-x",
        "model": "model-x",
        "pool_id": "pool-x",
        "policy_window_id": "window-x",
        "operation_id": operation,
        "unit": "provider-units",
        "quantity": quantity,
        "evidence_ref": "usage-evidence:" + operation,
        "evidence_sha256": "a" * 64,
        "reason": "Record provider usage result",
    }


def _claim_attempt(store, people, project, task):
    owner, worker = people["owner"], people["worker"]
    petri = task.get("metadata", {}).get("_skybuild_workflow", {}).get("petri")
    ready = store.task_action(owner, project, task["task_id"], "ready",
                              {"reason": "Reviewed usage test definition"},
                              task["revision"], "ready-" + task["task_id"])
    if not petri:
        task = store.initialize_workflow(owner, project, task["task_id"], ready["revision"],
                                         "initialize-" + task["task_id"])["task"]
    else:
        task = ready
    claim = store.claim_task(worker, project, task["task_id"], task["revision"],
                             "claim-" + task["task_id"])
    return store.get_task(owner, project, task["task_id"]), claim


def _release_attempt(store, people, project, task, claim):
    store.release_claim(people["worker"], project, task["task_id"], claim["fence"],
                        task["revision"], "release-" + task["task_id"],
                        reason="Usage source test attempt ended")
    return store.get_task(people["owner"], project, task["task_id"])


def _resolution(origin, *, operation="provider-reconcile-1", quantity="0.5"):
    return {
        "operation_id": operation,
        "quantity": quantity,
        "evidence_ref": "reconciled-evidence:" + operation,
        "evidence_sha256": "b" * 64,
        "reason": "Resolve provider usage exposure with retained evidence",
    }


def _error(code, call):
    with pytest.raises(DomainError) as caught:
        call()
    assert caught.value.code == code
    return caught.value


def test_canonical_usage_total_preserves_precision_beyond_decimal_context():
    assert _canonical("999999999999999999999999999999") == "999999999999999999999999999999"


def test_consumed_history_is_trusted_idempotent_and_survives_definition_edit(store, actors):
    project, people = actors
    task = create(store, people["owner"], project, "usage-edit", acceptance_criteria=["usage test"])
    task, claim = _claim_attempt(store, people, project, task)
    ordinary = people["worker"]
    recorder = _principal(store, project, "usage-recorder", {"tasks:usage-record"})
    body = _usage(task)

    _error("authorization", lambda: store.record_task_usage(
        ordinary, project, task["task_id"], body, "ordinary-worker"))
    _error("usage_conflict", lambda: store.record_task_usage(
        recorder, project, task["task_id"], {**body, "attempt_id": "unbound-attempt"},
        "unbound-attempt"))
    _error("usage_conflict", lambda: store.record_task_usage(
        recorder, project, task["task_id"], {**body, "definition_revision": body["definition_revision"] + 1},
        "unbound-definition"))
    first = store.record_task_usage(recorder, project, task["task_id"], body, "usage-record-1")
    assert store.record_task_usage(recorder, project, task["task_id"], body, "usage-record-1") == first
    with store._connection() as connection:
        task_event = connection.execute(
            "SELECT event_id FROM task_journal WHERE project_id = %s AND task_id = %s AND revision = %s",
            (project, task["task_id"], task["revision"]),
        ).fetchone()
    assert first["task_journal_event_id"] == str(task_event["event_id"])
    conflicting = {**body, "quantity": "0.8"}
    _error("usage_conflict", lambda: store.record_task_usage(
        recorder, project, task["task_id"], conflicting, "usage-record-2"))

    released = _release_attempt(store, people, project, task, claim)
    edited = store.update_task(people["owner"], project, task["task_id"],
                               {"description": "Revised definition"}, released["revision"], "edit-after-usage")
    history = store.task_usage_history(people["owner"], project, task["task_id"])
    assert edited["revision"] == released["revision"] + 1
    assert history["events"] == [first]
    assert history["events"][0]["task_revision"] == body["task_revision"]
    assert history["totals"] == [{
        "provider": "provider-x", "model": "model-x", "pool_id": "pool-x",
        "policy_window_id": "window-x", "unit": "provider-units",
        "consumed": "0.75", "uncertain": "0",
    }]


def test_uncertain_usage_blocks_replacement_until_different_trusted_resolver(store, actors):
    project, people = actors
    task = create(store, people["owner"], project, "usage-uncertain", acceptance_criteria=["usage test"])
    task, claim = _claim_attempt(store, people, project, task)
    recorder = _principal(store, project, "usage-recorder", {"tasks:usage-record", "tasks:usage-resolve"})
    resolver = _principal(store, project, "usage-resolver", {"tasks:usage-resolve"})
    origin = store.record_task_usage(
        recorder, project, task["task_id"],
        _usage(task, kind="uncertain", operation="provider-unknown-1", quantity="2.5"), "uncertain-1")
    task = _release_attempt(store, people, project, task, claim)

    _error("usage_conflict", lambda: store.update_task(
        people["owner"], project, task["task_id"], {"description": "Replacement"},
        task["revision"], "blocked-edit"))
    children = [
        {"task_id": "usage-blocked-child-a", "title": "A", "description": "Scope A",
         "acceptance_criteria": ["A"], "dependencies": []},
        {"task_id": "usage-blocked-child-b", "title": "B", "description": "Scope B",
         "acceptance_criteria": ["B"], "dependencies": []},
    ]
    _error("usage_conflict", lambda: store.split_task(
        people["owner"], project, task["task_id"], children, {}, "Must resolve exposure", task["revision"], "blocked-split"))
    other = create(store, people["owner"], project, "usage-merge-other")
    merge_target = {"task_id": "usage-blocked-merged", "title": "Merged", "description": "Combined",
                    "acceptance_criteria": ["A", "B"], "dependencies": []}
    _error("usage_conflict", lambda: store.merge_tasks(
        people["owner"], project, [task["task_id"], other["task_id"]], merge_target, [],
        {task["task_id"]: task["revision"], other["task_id"]: other["revision"]},
        "Must resolve exposure", "blocked-merge"))
    self_resolution = _resolution(origin)
    _error("authorization", lambda: store.resolve_task_usage(
        recorder, project, task["task_id"], origin["event_id"], self_resolution, "self-resolve"))

    resolution = store.resolve_task_usage(
        resolver, project, task["task_id"], origin["event_id"], self_resolution, "resolve-1")
    assert store.resolve_task_usage(
        resolver, project, task["task_id"], origin["event_id"], self_resolution, "resolve-1") == resolution
    edited = store.update_task(people["owner"], project, task["task_id"],
                               {"description": "Replacement after reconciliation"}, task["revision"], "edit-after-resolve")
    history = store.task_usage_history(people["owner"], project, task["task_id"])
    assert edited["revision"] == task["revision"] + 1
    assert {row["event_kind"] for row in history["events"]} == {"uncertain", "resolved"}
    assert len({row["event_id"] for row in history["events"]}) == 2
    assert history["totals"][0]["consumed"] == "0.5"
    assert history["totals"][0]["uncertain"] == "0"


def test_late_usage_resolution_keeps_original_task_revision_binding(store, actors):
    project, people = actors
    task = create(store, people["owner"], project, "usage-late", acceptance_criteria=["usage test"])
    task, claim = _claim_attempt(store, people, project, task)
    attempt_task = task
    released = _release_attempt(store, people, project, task, claim)
    edited = store.update_task(people["owner"], project, task["task_id"],
                               {"description": "New task definition"}, released["revision"], "edit-before-late-result")
    recorder = _principal(store, project, "usage-recorder", {"tasks:usage-record"})
    resolver = _principal(store, project, "usage-resolver", {"tasks:usage-resolve"})
    origin = store.record_task_usage(
        recorder, project, task["task_id"],
        _usage(attempt_task, kind="uncertain", operation="late-provider-result", quantity="3"), "late-usage")
    resolution = store.resolve_task_usage(
        resolver, project, task["task_id"], origin["event_id"], _resolution(origin), "late-resolution")
    history = store.task_usage_history(people["owner"], project, task["task_id"])

    assert edited["revision"] == released["revision"] + 1
    assert {row["task_revision"] for row in history["events"]} == {
        attempt_task["metadata"]["_skybuild_workflow"]["petri"]["attempt_binding"]["task_revision"]}
    assert resolution["definition_revision"] == origin["definition_revision"]
    assert resolution["attempt_id"] == origin["attempt_id"]


def test_split_and_merge_surface_deduplicated_original_usage(store, actors):
    project, people = actors
    owner = people["owner"]
    recorder = _principal(store, project, "usage-recorder", {"tasks:usage-record"})
    source = create(store, owner, project, "usage-source", acceptance_criteria=["first", "second"])
    source, source_claim = _claim_attempt(store, people, project, source)
    source_event = store.record_task_usage(
        recorder, project, source["task_id"],
        _usage(source, operation="provider-source", quantity="0.25"), "usage-source-1")
    children = [
        {"task_id": "usage-child-a", "title": "First", "description": "First scope",
         "acceptance_criteria": ["first"], "dependencies": []},
        {"task_id": "usage-child-b", "title": "Second", "description": "Second scope",
         "acceptance_criteria": ["second"], "dependencies": []},
    ]
    source = _release_attempt(store, people, project, source, source_claim)
    store.split_task(owner, project, source["task_id"], children, {},
                     "Split by acceptance", source["revision"], "usage-split")
    child_a, child_a_claim = _claim_attempt(
        store, people, project, store.get_task(owner, project, "usage-child-a"))
    child_b, child_b_claim = _claim_attempt(
        store, people, project, store.get_task(owner, project, "usage-child-b"))
    store.record_task_usage(recorder, project, child_a["task_id"],
                            _usage(child_a, operation="provider-child-a", quantity="1"), "usage-child-a-1")
    store.record_task_usage(recorder, project, child_b["task_id"],
                            _usage(child_b, operation="provider-child-b", quantity="2"), "usage-child-b-1")
    child_a = _release_attempt(store, people, project, child_a, child_a_claim)
    child_b = _release_attempt(store, people, project, child_b, child_b_claim)
    target = {"task_id": "usage-merged", "title": "Merged", "description": "Combined scope",
              "acceptance_criteria": ["first", "second"], "dependencies": []}
    store.merge_tasks(owner, project, ["usage-child-a", "usage-child-b"], target,
                      [], {"usage-child-a": child_a["revision"], "usage-child-b": child_b["revision"]},
                      "Merge linked scopes", "usage-merge")

    history = store.task_usage_history(owner, project, "usage-merged")
    event_ids = [row["event_id"] for row in history["events"]]
    assert len(event_ids) == len(set(event_ids)) == 3
    assert source_event["event_id"] in event_ids
    assert {row["task_id"] for row in history["events"]} == {
        "usage-source", "usage-child-a", "usage-child-b"}
    assert history["totals"][0]["consumed"] == "3.25"
    assert history["totals"][0]["uncertain"] == "0"


@pytest.mark.parametrize("statement", [
    "UPDATE skybuild.task_usage_events SET reason = 'rewritten' WHERE project_id = %s",
    "DELETE FROM skybuild.task_usage_events WHERE project_id = %s",
    "TRUNCATE skybuild.task_usage_events",
])
def test_usage_history_is_database_append_only(store, actors, statement):
    project, people = actors
    task = create(store, people["owner"], project, "usage-immutable", acceptance_criteria=["usage test"])
    task, _claim = _claim_attempt(store, people, project, task)
    recorder = _principal(store, project, "usage-recorder", {"tasks:usage-record"})
    store.record_task_usage(recorder, project, task["task_id"], _usage(task), "usage-immutable-1")
    import psycopg

    with psycopg.connect(store.dsn) as connection:
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            connection.execute(statement, (project,) if "%s" in statement else None)
        connection.rollback()


def test_usage_event_and_idempotency_roll_back_together(store, actors, monkeypatch):
    project, people = actors
    task = create(store, people["owner"], project, "usage-rollback", acceptance_criteria=["usage test"])
    task, _claim = _claim_attempt(store, people, project, task)
    recorder = _principal(store, project, "usage-recorder", {"tasks:usage-record"})
    body = _usage(task, operation="provider-rollback")
    original = store._idempotent

    def fail_after_append(*args, **kwargs):
        original(*args, **kwargs)
        raise DomainError("injected", "Rollback after usage event append", 409)

    monkeypatch.setattr(store, "_idempotent", fail_after_append)
    _error("injected", lambda: store.record_task_usage(
        recorder, project, task["task_id"], body, "usage-rollback-key"))
    monkeypatch.setattr(store, "_idempotent", original)
    with store._connection() as connection:
        assert connection.execute(
            "SELECT count(*) AS count FROM task_usage_events WHERE project_id = %s AND task_id = %s",
            (project, task["task_id"]),
        ).fetchone()["count"] == 0
        assert connection.execute(
            "SELECT count(*) AS count FROM idempotency WHERE project_id = %s AND idempotency_key = %s",
            (project, "usage-rollback-key"),
        ).fetchone()["count"] == 0
        assert connection.execute(
            "SELECT count(*) AS count FROM task_usage_events u JOIN task_journal j "
            "ON j.event_id = u.task_journal_event_id WHERE u.project_id = %s AND u.task_id = %s",
            (project, task["task_id"]),
        ).fetchone()["count"] == 0
