"""Conservative task enrollment with one versioned required-check profile."""
from datetime import datetime, timezone

from .completion import current_completion
from .workflow import Place, TaskToken, ValidationStage

DEFAULT_POLICY_VERSION = "petri-checks-v1"
DEFAULT_REQUIREMENTS = tuple(ValidationStage)


def enrollment_token(task, *, input_generation, revision, new=False):
    """Require evidence before restoring execution from legacy state.

    A phase name cannot prove an attempt, claim, current review or publication.
    Ambiguous legacy work therefore remains in Hold for explicit reassessment.
    """
    trigger = task.get("metadata", {}).get("_skybuild_workflow", {}).get("deferral", {})
    until = trigger.get("until")
    try:
        dated = datetime.fromisoformat(until) if isinstance(until, str) else None
        valid_date = bool(dated and dated.utcoffset() is not None)
    except ValueError:
        valid_date = False
    valid_trigger = valid_date or bool(trigger.get("milestone_task_id"))
    defined = bool(task.get("title") and task.get("description") and task.get("acceptance_criteria"))
    place = Place.HOLD
    reason = "Review legacy task definition and evidence"
    if new:
        place = Place.READY if defined else Place.HOLD
        reason = None if defined else "Add acceptance criteria and review the task definition"
    elif current_completion(task):
        reason = "Legacy acceptance is retained in history; verify current Petri inputs and required stages"
    elif task["status"] == "ready" and defined:
        place, reason = Place.READY, None
    elif task["status"] == "deferred" and valid_trigger:
        place, reason = Place.DEFERRED, trigger.get("reason") or task.get("blocker") or "Explicit owner deferral"
    elif task["status"] == "superseded":
        reason = "Superseded scope; inspect replacement links before any correction"
    return TaskToken(task["project_id"], task["task_id"], place, title=task["title"],
                     priority=task["priority"], dependencies=tuple(task.get("dependencies", ())),
                     definition_revision=task["revision"], input_generation=input_generation,
                     revision=revision, responsible=task["responsible"],
                     next_action=task.get("next_action") or "", blocker=task.get("blocker"),
                     hold_reason=reason, deferred_until=until if place == Place.DEFERRED and valid_date else None,
                     milestone_task_id=trigger.get("milestone_task_id") if place == Place.DEFERRED else None,
                     superseded=task["status"] == "superseded", policy_version=DEFAULT_POLICY_VERSION,
                     requirements=DEFAULT_REQUIREMENTS)


def install_token(metadata, token):
    """Attach the authoritative token without replacing unrelated metadata."""
    metadata.setdefault("_skybuild_workflow", {})["petri"] = {
        "schema_version": 1, "token": token.to_dict(),
        "place_entered_at": datetime.now(timezone.utc).isoformat(),
    }
