"""Pure reconciliation of existing task tokens after material input changes."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone

from .workflow import Place, ResultState, TaskToken


STATUS_BY_PLACE = {
    Place.READY: "ready", Place.WORKING: "in-progress", Place.VALIDATING: "in-progress",
    Place.INTEGRATING: "in-progress", Place.DONE: "done", Place.DEFERRED: "deferred", Place.HOLD: "blocked",
}


def synchronize_token(before, values, *, operation, reason):
    """Invalidate token evidence without restoring permissions or owner choices.

    The caller retains the graph lock, exposure checks and readiness transaction.
    Legacy tasks keep their existing behavior until explicit workflow enrollment.
    """
    workflow = values["metadata"].get("_skybuild_workflow", {})
    petri = workflow.get("petri", {})
    if petri.get("schema_version") != 1:
        return
    body = deepcopy(petri["token"])
    token = TaskToken.from_dict(body)
    marker = workflow["readiness"]
    changed = marker["input_generation"] != token.input_generation
    place = token.place
    superseded = token.superseded or operation in {"split", "merge"}
    if superseded:
        place = Place.HOLD
    elif operation == "completed":
        place = Place.DONE
    elif operation == "ready":
        place = Place.READY
    elif operation in {"rework", "resume"}:
        place = Place.READY
    elif operation == "reassess" and place not in {Place.HOLD, Place.DEFERRED}:
        from .completion import current_completion
        if place != Place.DONE or not current_completion(before):
            place = Place.READY
    changes = dict(place=place, superseded=superseded,
                   input_generation=marker["input_generation"], revision=before["revision"] + 1)
    if changed:
        changes.update(evidence=tuple(replace(item, state=ResultState.STALE) for item in token.evidence))
        if operation == "updated":
            changes["definition_revision"] = before["revision"] + 1
    if (place in {Place.HOLD, Place.DEFERRED} and not superseded
            and operation not in {"defer", "update_control"}):
        # A definition or dependency edit cannot silently release owner control.
        values.update(status=STATUS_BY_PLACE[place], phase=place.value,
                      blocker=token.hold_reason or before["blocker"], next_action=before["next_action"])
        old = before["metadata"].get("_skybuild_workflow", {})
        if "deferral" in old:
            workflow["deferral"] = deepcopy(old["deferral"])
    else:
        values.update(status="superseded" if superseded else STATUS_BY_PLACE[place], phase=place.value)
    if operation in {"rework", "resume", "ready"}:
        changes.update(hold_reason=None, deferred_until=None, milestone_task_id=None,
                       interrupted_place=None, pending_action=None)
    if superseded:
        changes.update(hold_reason=reason, pending_action=None)
    changes.update(title=values["title"], priority=values["priority"],
                   dependencies=tuple(values["dependencies"]), responsible=values["responsible"],
                   next_action=values["next_action"] or "", blocker=values["blocker"])
    petri["token"] = replace(token, **changes).to_dict()
    if place.value != before["metadata"]["_skybuild_workflow"]["petri"]["token"]["place"]:
        petri["place_entered_at"] = datetime.now(timezone.utc).isoformat()


def replacement_token(project_id, task_id, values):
    """Create a replacement identity without inheriting execution or evidence."""
    token = TaskToken(project_id, task_id, Place.READY, title=values["title"],
                      priority=values["priority"], dependencies=tuple(values["dependencies"]),
                      definition_revision=1, input_generation=1, revision=1,
                      responsible=values["responsible"], next_action="Reassess replacement requirements and dependencies")
    values.update(status="ready", phase="ready", next_action=token.next_action)
    values["metadata"]["_skybuild_workflow"]["petri"] = {
        "schema_version": 1, "token": token.to_dict(),
        "place_entered_at": datetime.now(timezone.utc).isoformat(),
    }
