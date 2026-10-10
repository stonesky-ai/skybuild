"""Bounded task records and manual, non-executing workflow actions."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from dataclasses import dataclass, fields
from enum import Enum
import json

from .contracts import DomainError, valid_identifier



class Place(str, Enum):
    READY = "ready"
    WORKING = "working"
    VALIDATING = "validating"
    INTEGRATING = "integrating"
    DONE = "done"
    DEFERRED = "deferred"
    HOLD = "hold"


class ValidationStage(str, Enum):
    UNIT_TESTS = "unit_tests"
    SCANS = "scans"
    LONG_TESTS = "long_tests"
    CODE_REVIEW = "code_review"
    NEEDS_REBASE = "needs_rebase"


class ResultState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    STALE = "stale"
    NOT_APPLICABLE = "not_applicable"


def _record_error(message: str) -> None:
    raise DomainError("validation", message, 422)


def _text_field(value, name, maximum=4096, optional=False, identifier=False):
    if optional and value is None:
        return
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
        _record_error(f"Invalid workflow {name}")
    if identifier and not valid_identifier(value):
        _record_error(f"Invalid workflow {name}")


def _counter(value, name, optional=False, signed=False):
    if optional and value is None:
        return
    minimum = -(2**31) if signed else 0
    if type(value) is not int or not minimum <= value < 2**31:
        _record_error(f"Invalid workflow {name}")


def _items(value, name, identifier=False):
    if not isinstance(value, tuple) or len(value) > 100:
        _record_error(f"Workflow {name} requires at most 100 immutable items")
    for item in value:
        _text_field(item, name, identifier=identifier)


def _json_value(value):
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (TaskToken, ValidationResult, TransitionSpec)):
        return {field.name: _json_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


def _record_dict(record):
    value = _json_value(record)
    try:
        encoded = json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":")).encode()
    except (TypeError, ValueError, RecursionError, UnicodeError):
        _record_error("Workflow record must be finite JSON")
    if len(encoded) > 16_384:
        _record_error("Workflow record exceeds 16 KiB")
    return value


def _record_input(cls, value):
    if not isinstance(value, dict) or set(value) - {field.name for field in fields(cls)}:
        _record_error("Unsupported workflow record or field")
    # Check the outer size before constructing nested records.
    try:
        encoded = json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":")).encode()
    except (TypeError, ValueError, RecursionError, UnicodeError):
        _record_error("Workflow record must be finite JSON")
    if len(encoded) > 16_384:
        _record_error("Workflow record exceeds 16 KiB")
    return dict(value)


def _enum_value(enum, value):
    try:
        return enum(value)
    except (TypeError, ValueError):
        _record_error(f"Invalid workflow {enum.__name__}")


@dataclass(frozen=True)
class ValidationResult:
    """One bounded result for exact task inputs. Logs remain in artifact storage."""

    project_id: str
    task_id: str
    stage: ValidationStage
    state: ResultState
    attempt_id: str | None = None
    source_head: str | None = None
    target_base: str | None = None
    input_generation: int = 0
    definition_revision: int = 0
    policy_version: str = ""
    claim_fence: int | None = None
    producer: str = ""
    check_id: str = ""
    parameters: tuple[tuple[str, str], ...] = ()
    tool_version: str = ""
    findings: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()
    policy_reason: str | None = None

    def __post_init__(self):
        for name in ("project_id", "task_id"):
            _text_field(getattr(self, name), name, identifier=True)
        if not isinstance(self.stage, ValidationStage) or not isinstance(self.state, ResultState):
            _record_error("Validation result requires stage and state enums")
        _counter(self.input_generation, "input_generation")
        _counter(self.definition_revision, "definition_revision")
        _counter(self.claim_fence, "claim_fence", optional=True)
        for name in ("attempt_id", "source_head", "target_base", "policy_reason"):
            _text_field(getattr(self, name), name, optional=True)
        for name in ("producer", "check_id", "tool_version", "policy_version"):
            _text_field(getattr(self, name), name, maximum=200)
        _items(self.findings, "findings")
        _items(self.artifacts, "artifacts")
        if not isinstance(self.parameters, tuple) or len(self.parameters) > 100:
            _record_error("Parameters require at most 100 immutable pairs")
        for pair in self.parameters:
            if not isinstance(pair, tuple) or len(pair) != 2:
                _record_error("Parameters require immutable string pairs")
            for item in pair:
                _text_field(item, "parameter")
        if len({pair[0] for pair in self.parameters}) != len(self.parameters):
            _record_error("Parameter names must be unique")
        _record_dict(self)

    def to_dict(self) -> dict:
        return _record_dict(self)

    @classmethod
    def from_dict(cls, value: dict) -> ValidationResult:
        body = _record_input(cls, value)
        for name, enum in (("stage", ValidationStage), ("state", ResultState)):
            body[name] = _enum_value(enum, body.get(name))
        for name in ("findings", "artifacts", "parameters"):
            if name in body:
                if not isinstance(body[name], list):
                    _record_error(f"Workflow {name} must be a JSON array")
                body[name] = tuple(body[name])
        if "parameters" in body:
            if any(not isinstance(pair, list) for pair in body["parameters"]):
                _record_error("Parameters require JSON array pairs")
            body["parameters"] = tuple(tuple(pair) for pair in body["parameters"])
        return _construct_record(cls, body)


@dataclass(frozen=True)
class TaskToken:
    """Immutable task view. This record does not grant execution permission."""

    project_id: str
    task_id: str
    place: Place = Place.HOLD
    title: str = ""
    priority: int = 0
    links: tuple[str, ...] = ()
    source_branch: str | None = None
    source_head: str | None = None
    target_base: str | None = None
    definition_revision: int = 0
    input_generation: int = 0
    policy_version: str = ""
    requirements: tuple[ValidationStage, ...] = ()
    dependencies: tuple[str, ...] = ()
    evidence: tuple[ValidationResult, ...] = ()
    findings: tuple[str, ...] = ()
    faults: tuple[str, ...] = ()
    next_action: str = ""
    responsible: str = "owner"
    blocker: str | None = None
    hold_reason: str | None = None
    deferred_until: str | None = None
    milestone_task_id: str | None = None
    interrupted_place: Place | None = None
    attempt_id: str | None = None
    claim_fence: int | None = None
    bundle_id: str | None = None
    superseded: bool = False
    pending_action: str | None = None
    revision: int = 0

    def __post_init__(self):
        for name in ("project_id", "task_id"):
            _text_field(getattr(self, name), name, identifier=True)
        if not isinstance(self.place, Place) or (self.interrupted_place is not None and
                                               not isinstance(self.interrupted_place, Place)):
            _record_error("Task token requires place enums")
        for name in ("definition_revision", "input_generation", "revision"):
            _counter(getattr(self, name), name)
        _counter(self.priority, "priority", signed=True)
        _counter(self.claim_fence, "claim_fence", optional=True)
        if type(self.superseded) is not bool:
            _record_error("Superseded must be a boolean")
        _text_field(self.title, "title", maximum=500)
        for name in ("policy_version", "responsible"):
            _text_field(getattr(self, name), name, maximum=200)
        _text_field(self.next_action, "next_action")
        for name in ("source_branch", "source_head", "target_base", "blocker", "hold_reason",
                     "deferred_until", "milestone_task_id", "attempt_id", "bundle_id", "pending_action"):
            _text_field(getattr(self, name), name, optional=True)
        for name in ("links", "findings", "faults", "dependencies"):
            _items(getattr(self, name), name, identifier=name == "dependencies")
        if (not isinstance(self.requirements, tuple) or len(self.requirements) > len(ValidationStage) or
                any(not isinstance(stage, ValidationStage) for stage in self.requirements) or
                len(set(self.requirements)) != len(self.requirements)):
            _record_error("Requirements must contain unique validation stages")
        if not isinstance(self.evidence, tuple) or len(self.evidence) > 100:
            _record_error("Evidence requires at most 100 immutable results")
        for result in self.evidence:
            if not isinstance(result, ValidationResult) or (result.project_id, result.task_id) != (self.project_id, self.task_id):
                _record_error("Evidence must belong to this task")
        _record_dict(self)

    def to_dict(self) -> dict:
        return _record_dict(self)

    @classmethod
    def from_dict(cls, value: dict) -> TaskToken:
        body = _record_input(cls, value)
        for name in ("place", "interrupted_place"):
            if name in body and body[name] is not None:
                body[name] = _enum_value(Place, body[name])
        for name in ("links", "requirements", "dependencies", "evidence", "findings", "faults"):
            if name in body:
                if not isinstance(body[name], list):
                    _record_error(f"Workflow {name} must be a JSON array")
                body[name] = tuple(body[name])
        if "requirements" in body:
            body["requirements"] = tuple(_enum_value(ValidationStage, stage) for stage in body["requirements"])
        if "evidence" in body:
            body["evidence"] = tuple(ValidationResult.from_dict(result) for result in body["evidence"])
        return _construct_record(cls, body)


@dataclass(frozen=True)
class TransitionSpec:
    """Declare source places, a fixed destination and a named guard.

    A null destination keeps the current place. The kernel resolves guards.
    """

    event: str
    sources: tuple[Place, ...]
    destination: Place | None
    guard: str

    def __post_init__(self):
        _text_field(self.event, "event", maximum=200, identifier=True)
        _text_field(self.guard, "guard", maximum=200, identifier=True)
        if (not isinstance(self.sources, tuple) or not self.sources or len(self.sources) > len(Place) or
                any(not isinstance(place, Place) for place in self.sources) or
                len(set(self.sources)) != len(self.sources)):
            _record_error("Transition sources must contain unique places")
        if self.destination is not None and not isinstance(self.destination, Place):
            _record_error("Transition destination must be a place or null")
        _record_dict(self)

    def to_dict(self) -> dict:
        return _record_dict(self)

    @classmethod
    def from_dict(cls, value: dict) -> TransitionSpec:
        body = _record_input(cls, value)
        if not isinstance(body.get("sources"), list):
            _record_error("Transition sources must be a JSON array")
        body["sources"] = tuple(_enum_value(Place, place) for place in body["sources"])
        if body.get("destination") is not None:
            body["destination"] = _enum_value(Place, body["destination"])
        return _construct_record(cls, body)


def _construct_record(cls, body):
    try:
        return cls(**body)
    except TypeError:
        _record_error("Missing or invalid workflow record fields")


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
