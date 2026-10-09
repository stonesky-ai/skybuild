"""Manual, non-executing task transitions for the early workbench."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

from .contracts import DomainError, valid_identifier

ACTION_FIELDS = frozenset({"reason", "next_action", "responsible", "until", "milestone_task_id"})
ACTIONS = frozenset({"rework", "reassess", "defer", "resume", "ready"})


def action_change(before: dict, action: str, body: dict) -> dict:
    """Return fields to change, leaving admission and execution untouched."""
    if action not in ACTIONS or not isinstance(body, dict) or set(body) - ACTION_FIELDS:
        raise DomainError("validation", "Unsupported task action or field", 422)
    reason = body.get("reason")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 4096:
        raise DomainError("validation", "Action requires a bounded reason", 422)
    if action != "defer" and ("until" in body or "milestone_task_id" in body):
        raise DomainError("validation", "Only deferral accepts a trigger", 422)
    if action == "ready" and "next_action" in body:
        raise DomainError("validation", "Ready tasks await separate admission", 422)
    if action == "defer":
        if before["status"] == "done":
            raise DomainError("workflow_conflict", "Rework completed task before deferral", 409)
        if ("until" in body) == ("milestone_task_id" in body):
            raise DomainError("validation", "Deferral requires exactly one date or milestone", 422)
        if "until" in body:
            try:
                value = datetime.fromisoformat(body["until"].replace("Z", "+00:00"))
            except (AttributeError, TypeError, ValueError):
                raise DomainError("validation", "Deferral date must include a timezone", 422) from None
            if value.tzinfo is None or value.utcoffset() is None:
                raise DomainError("validation", "Deferral date must include a timezone", 422)
            if value <= datetime.now(timezone.utc):
                raise DomainError("workflow_conflict", "Deferral date has already passed", 409)
            trigger = {"until": value.isoformat()}
        else:
            milestone = body["milestone_task_id"]
            if not valid_identifier(milestone) or milestone == before["task_id"]:
                raise DomainError("validation", "Invalid deferral milestone", 422)
            trigger = {"milestone_task_id": milestone}
    elif action == "resume":
        if before["status"] != "deferred":
            raise DomainError("workflow_conflict", "Only a deferred task can resume", 409)
        trigger = None
    elif action == "ready":
        if before["status"] not in {"proposed", "blocked"}:
            raise DomainError("workflow_conflict", "Only proposed or blocked tasks can be marked ready", 409)
        trigger = None
    else:
        if before["status"] == "deferred":
            raise DomainError("workflow_conflict", "Resume deferred task before changing its work phase", 409)
        trigger = None

    responsible = body.get("responsible", before["responsible"])
    if not isinstance(responsible, str) or not responsible.strip() or len(responsible) > 200:
        raise DomainError("validation", "Action requires a responsible owner", 422)
    next_action = body.get("next_action")
    if next_action is None:
        next_action = ("Review task after resume" if action == "resume" else
                       "Await explicit admission and ownership" if action == "ready" else f"Resolve {action} request")
    if not isinstance(next_action, str) or not next_action.strip() or len(next_action) > 4096:
        raise DomainError("validation", "Action requires a concrete next action", 422)

    metadata = deepcopy(before["metadata"])
    workflow = metadata.setdefault("_skybuild_workflow", {})
    if not isinstance(workflow, dict):
        raise DomainError("workflow_conflict", "Reserved workflow metadata has an invalid shape", 409)
    generation = workflow.get("generation", 0)
    if type(generation) is not int or generation < 0 or generation >= 2**31 - 1:
        raise DomainError("workflow_conflict", "Workflow generation is invalid", 409)
    workflow["generation"] = generation + 1
    workflow["last_action"] = action
    workflow["reason"] = reason
    if action == "defer":
        if before["status"] != "deferred":
            workflow["interrupted_status"] = before["status"]
            workflow["interrupted_phase"] = before["phase"]
        workflow["deferral"] = {"reason": reason, **trigger}
    elif action == "resume":
        workflow.pop("deferral", None)

    if action == "defer":
        return {"status": "deferred", "phase": "deferred", "blocker": reason,
                "next_action": next_action, "responsible": responsible, "metadata": metadata}
    if action == "resume":
        return {"status": "blocked", "phase": "reassess", "blocker": reason,
                "next_action": next_action, "responsible": responsible, "metadata": metadata}
    if action == "ready":
        return {"status": "ready", "phase": "ready-for-work", "blocker": None,
                "next_action": next_action, "responsible": responsible, "metadata": metadata}
    return {"status": "blocked", "phase": "needs-rework" if action == "rework" else "reassess",
            "blocker": reason, "next_action": next_action, "responsible": responsible, "metadata": metadata}


def due_deferral(task: dict, now: datetime, milestone_done: bool) -> bool:
    """Recognize a reached trigger without changing state or querying a model."""
    trigger = task.get("metadata", {}).get("_skybuild_workflow", {}).get("deferral", {})
    if not isinstance(trigger, dict):
        return False
    if "milestone_task_id" in trigger:
        return milestone_done
    if "until" in trigger:
        try:
            value = datetime.fromisoformat(trigger["until"])
        except (TypeError, ValueError):
            return False
        return value.tzinfo is not None and value.utcoffset() is not None and value <= now
    return False
