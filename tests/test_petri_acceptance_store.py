"""Final task acceptance on an explicitly selected disposable PostgreSQL database."""
from copy import deepcopy
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

from skybuild.contracts import DomainError
from skybuild.enrollment import DEFAULT_POLICY_VERSION, DEFAULT_REQUIREMENTS
from skybuild.store import Store
from skybuild.workflow import Place
from test_store import actors, create, seed_api_authority, store
from test_petri_store import author_receipt


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
