"""Bounded task records and manual, non-executing workflow actions."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from dataclasses import dataclass, fields, replace
from enum import Enum
import json
from typing import TypedDict

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
    maximum = 2**31 if signed else 2**63
    if type(value) is not int or not minimum <= value < maximum:
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
    policy_reason: str | None = None
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
                     "deferred_until", "milestone_task_id", "attempt_id", "bundle_id", "pending_action", "policy_reason"):
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


class WorkflowEvent(TypedDict, total=False):
    """Bounded mutation input. Store verifies authority and operation identity."""

    event: str
    operation_id: str
    expected_revision: int
    reason: str
    next_action: str
    responsible: str
    until: str
    milestone_task_id: str
    result: dict


class WorkflowContext(TypedDict, total=False):
    """Trusted facts read by Store in the same transaction as the mutation.

    Never copy these facts from a caller request. The pure kernel cannot verify
    credentials, perform claims, check publication or resolve external effects.
    """

    source_head: str | None
    target_base: str | None
    definition_revision: int
    input_generation: int
    policy_version: str
    current_inputs: bool
    dependencies_satisfied: bool
    admission_permitted: bool
    effects_resolved: bool
    claim_live: bool
    attempt_id: str
    claim_fence: int
    responsible: str
    submission_verified: bool
    validation_verified: bool
    integration_fixed: bool
    bundle_id: str | None
    publication_required: bool
    acceptance_verified: bool
    publication_verified: bool
    task_included: bool
    policy_reason: str
    result_authorized: bool
    control_authorized: bool
    resume_due: bool
    failure_confirmed: bool
    now: datetime


_ACTIVE_PLACES = (Place.READY, Place.WORKING, Place.VALIDATING, Place.INTEGRATING)
_CONTROL_EVENTS = frozenset({"validation_result", "validation_failure", "integration_failure",
                             "work_failure", "hold", "release_hold", "defer", "resume_deferred",
                             "reopen", "update_control"})
TRANSITIONS = (
    TransitionSpec("claim", (Place.READY,), Place.WORKING, "claim"),
    TransitionSpec("submit", (Place.WORKING,), Place.VALIDATING, "submit"),
    TransitionSpec("freeze", (Place.VALIDATING,), Place.INTEGRATING, "freeze"),
    TransitionSpec("accept", (Place.INTEGRATING,), Place.DONE, "accept"),
    TransitionSpec("validation_result", (Place.VALIDATING,), None, "validation_result"),
    TransitionSpec("validation_failure", (Place.VALIDATING,), Place.READY, "validation_failure"),
    TransitionSpec("integration_failure", (Place.INTEGRATING,), Place.READY, "integration_failure"),
    TransitionSpec("work_failure", (Place.WORKING,), Place.READY, "work_failure"),
    TransitionSpec("hold", _ACTIVE_PLACES + (Place.DEFERRED,), Place.HOLD, "hold"),
    TransitionSpec("release_hold", (Place.HOLD,), Place.READY, "release_hold"),
    TransitionSpec("defer", _ACTIVE_PLACES + (Place.HOLD,), Place.DEFERRED, "defer"),
    TransitionSpec("resume_deferred", (Place.DEFERRED,), Place.READY, "resume_deferred"),
    TransitionSpec("reopen", (Place.DONE,), Place.READY, "reopen"),
    TransitionSpec("update_control", (Place.HOLD, Place.DEFERRED), None, "update_control"),
)


def _workflow_event(value: WorkflowEvent) -> dict:
    if not isinstance(value, dict) or set(value) - WorkflowEvent.__annotations__.keys():
        _record_error("Unsupported workflow event or field")
    for name in ("event", "operation_id"):
        _text_field(value.get(name), name, maximum=200, identifier=True)
    _counter(value.get("expected_revision"), "expected_revision")
    for name in ("reason", "next_action", "responsible", "until", "milestone_task_id"):
        if name in value:
            _text_field(value[name], name, maximum=200 if name == "responsible" else 4096)
    if "result" in value:
        ValidationResult.from_dict(value["result"])
    try:
        size = len(json.dumps(value, allow_nan=False, ensure_ascii=False).encode())
    except (TypeError, ValueError, RecursionError, UnicodeError):
        _record_error("Workflow event must be finite JSON")
    if size > 16_384:
        _record_error("Workflow event exceeds 16 KiB")
    return value


class TaskWorkflow:
    """Apply guarded changes without storage, dispatch or external operations.

    Store must commit the returned token, journal facts and claim effects in one
    transaction. Store also owns permission checks and idempotency replay.
    """

    @staticmethod
    def _guard(token: TaskToken, name: str, context: WorkflowContext) -> bool:
        if name in _CONTROL_EVENTS:
            return _control_guard(token, name, context)
        if not isinstance(context, dict) or token.superseded or token.pending_action is not None:
            return False
        if context.get("current_inputs") is not True or context.get("effects_resolved") is not True:
            return False
        for field in ("source_head", "target_base", "definition_revision", "input_generation", "policy_version"):
            if field not in context or type(context[field]) is not type(getattr(token, field)) or context[field] != getattr(token, field):
                return False
        if name == "claim":
            return (context.get("dependencies_satisfied") is True and
                    context.get("admission_permitted") is True and
                    context.get("claim_live") is True and
                    valid_identifier(context.get("attempt_id")) and
                    type(context.get("claim_fence")) is int and 0 < context["claim_fence"] < 2**63 and
                    isinstance(context.get("responsible"), str) and
                    bool(context["responsible"].strip()) and len(context["responsible"]) <= 200 and
                    "\x00" not in context["responsible"])
        if name == "submit":
            return (context.get("claim_live") is True and context.get("submission_verified") is True and
                    token.attempt_id is not None and token.claim_fence is not None and
                    context.get("attempt_id") == token.attempt_id and
                    type(context.get("claim_fence")) is int and context["claim_fence"] == token.claim_fence)
        if name == "freeze":
            if context.get("validation_verified") is not True or context.get("integration_fixed") is not True:
                return False
            if context.get("publication_required") is True:
                return valid_identifier(context.get("bundle_id"))
            return (context.get("publication_required") is False and
                    isinstance(context.get("policy_reason"), str) and bool(context["policy_reason"].strip()) and
                    len(context["policy_reason"]) <= 4096 and "\x00" not in context["policy_reason"])
        if name == "accept":
            if context.get("acceptance_verified") is not True:
                return False
            if context.get("publication_required") is True:
                return (token.bundle_id is not None and context.get("bundle_id") == token.bundle_id and
                        context.get("publication_verified") is True and context.get("task_included") is True)
            return (context.get("publication_required") is False and
                    isinstance(context.get("policy_reason"), str) and bool(context["policy_reason"].strip()) and
                    len(context["policy_reason"]) <= 4096 and "\x00" not in context["policy_reason"])
        return False

    def enabled(self, token: TaskToken, context: WorkflowContext) -> tuple[str, ...]:
        """List available event names from the same rules used by apply."""
        return tuple(spec.event for spec in TRANSITIONS
                     if token.place in spec.sources and self._guard(token, spec.guard, context))

    def apply(self, token: TaskToken, event: WorkflowEvent, context: WorkflowContext) -> TaskToken:
        event = _workflow_event(event)
        if event["expected_revision"] != token.revision:
            raise DomainError("workflow_conflict", "Task revision has changed", 409)
        spec = next((item for item in TRANSITIONS if item.event == event["event"]), None)
        if spec is None or token.place not in spec.sources or not self._guard(token, spec.guard, context):
            raise DomainError("workflow_conflict", "Workflow transition is not enabled", 409)
        if spec.event in _CONTROL_EVENTS:
            return _apply_control(token, event, context, spec)
        # Normal-path events have no caller-controlled detail fields.
        if set(event) != {"event", "operation_id", "expected_revision"}:
            _record_error("Unsupported fields for normal workflow transition")
        changes = {"place": spec.destination or token.place, "revision": token.revision + 1}
        if spec.event == "claim":
            changes.update(attempt_id=context["attempt_id"], claim_fence=context["claim_fence"],
                           responsible=context["responsible"])
        elif spec.event == "freeze":
            changes["bundle_id"] = context.get("bundle_id") if context["publication_required"] else None
        if spec.event in {"freeze", "accept"}:
            changes["policy_reason"] = None if context["publication_required"] else context["policy_reason"]
        return replace(token, **changes)

    @staticmethod
    def journal_facts(before: TaskToken, after: TaskToken, event: WorkflowEvent) -> dict:
        """Return bounded facts for the journal committed with the token.

        This method does not attest authority or persist a journal event.
        """
        event = _workflow_event(event)
        if ((before.project_id, before.task_id) != (after.project_id, after.task_id) or
                after.revision != before.revision + 1 or event["expected_revision"] != before.revision):
            _record_error("Journal facts require one revision of the same task")
        facts = {"project_id": after.project_id, "task_id": after.task_id,
                "operation_id": event["operation_id"], "event": event["event"],
                "from_place": before.place.value, "to_place": after.place.value,
                "revision": after.revision, "input_generation": after.input_generation,
                "attempt_id": after.attempt_id, "claim_fence": after.claim_fence,
                "source_head": after.source_head, "target_base": after.target_base,
                "definition_revision": after.definition_revision, "policy_version": after.policy_version,
                "bundle_id": after.bundle_id, "pending_action": after.pending_action,
                "policy_reason": after.policy_reason,
                "acceptance_mode": ("without_publication" if after.policy_reason is not None else "publication")
                if after.place in {Place.INTEGRATING, Place.DONE} else None}
        facts.update({name: event[name] for name in ("reason", "result", "until", "milestone_task_id")
                      if name in event})
        return facts


def _control_guard(token, name, context):
    if not isinstance(context, dict) or token.superseded or context.get("current_inputs") is not True:
        return False
    for field in ("source_head", "target_base", "definition_revision", "input_generation", "policy_version"):
        if field not in context or type(context[field]) is not type(getattr(token, field)) or context[field] != getattr(token, field):
            return False
    if name == "validation_result":
        return context.get("result_authorized") is True
    if token.pending_action is not None and name not in {
            token.pending_action, "validation_failure", "integration_failure", "work_failure"}:
        return False
    if context.get("control_authorized") is not True and not (name == "resume_deferred" and context.get("resume_due") is True):
        return False
    if name in {"validation_failure", "integration_failure"}:
        return context.get("failure_confirmed") is True
    if name == "work_failure":
        return context.get("failure_confirmed") is True or context.get("effects_resolved") is True
    if name in {"release_hold", "resume_deferred", "reopen"}:
        return context.get("effects_resolved") is True
    return token.pending_action is None or token.pending_action == name


def _result_current(token, result):
    return all(type(getattr(result, name)) is type(getattr(token, name)) and
               getattr(result, name) == getattr(token, name)
               for name in ("project_id", "task_id", "attempt_id", "claim_fence", "source_head",
                            "target_base", "definition_revision", "input_generation", "policy_version"))


def _stale_evidence(token):
    return tuple(replace(result, state=ResultState.STALE) for result in token.evidence)


def _defer_trigger(token, event, context):
    if ("until" in event) == ("milestone_task_id" in event):
        _record_error("Deferral requires exactly one date or milestone")
    if "until" in event:
        try:
            value = datetime.fromisoformat(event["until"].replace("Z", "+00:00"))
        except (TypeError, ValueError):
            _record_error("Deferral date must include a timezone")
        if value.tzinfo is None or value.utcoffset() is None:
            _record_error("Deferral date must include a timezone")
        now = context.get("now")
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            _record_error("Deferral requires a trusted offset-aware current time")
        accepted_pending = (token.pending_action == "defer" and event.get("reason") == token.hold_reason and
                            event.get("responsible", token.responsible) == token.responsible)
        unchanged = value.isoformat() == token.deferred_until and (accepted_pending or token.place == Place.DEFERRED)
        if value <= now and not unchanged:
            raise DomainError("workflow_conflict", "Deferral date has already passed", 409)
        return {"deferred_until": value.isoformat(), "milestone_task_id": None}
    milestone = event["milestone_task_id"]
    if not valid_identifier(milestone) or milestone == token.task_id:
        _record_error("Invalid deferral milestone")
    return {"deferred_until": None, "milestone_task_id": milestone}


def _apply_control(token, event, context, spec):
    name = spec.event
    allowed = {"event", "operation_id", "expected_revision"}
    allowed |= {"result"} if name == "validation_result" else {"reason", "next_action", "responsible"}
    if name in {"defer", "update_control"}:
        allowed |= {"until", "milestone_task_id"}
    if set(event) - allowed:
        _record_error("Unsupported fields for workflow transition")
    changes = {"place": spec.destination or token.place, "revision": token.revision + 1}
    if name == "validation_result":
        if "result" not in event:
            _record_error("Validation progress requires a result")
        result = ValidationResult.from_dict(event["result"])
        if not _result_current(token, result):
            raise DomainError("workflow_conflict", "Validation result inputs are stale", 409)
        if not result.producer.strip() or not result.check_id.strip():
            _record_error("Validation result requires a producer and check identity")
        if result.state == ResultState.STALE:
            _record_error("Stale results belong to their original history")
        if result.state == ResultState.NOT_APPLICABLE and not (result.policy_reason and result.policy_reason.strip()):
            _record_error("Not-applicable validation requires a policy reason")
        identity = lambda item: (item.stage, item.check_id, item.parameters, item.tool_version)
        results = tuple(item for item in token.evidence if identity(item) != identity(result)) + (result,)
        changes.update(evidence=results, findings=tuple(dict.fromkeys(token.findings + result.findings)))
        if result.state == ResultState.FAILED:
            # Keep the full findings in the result and journal. Repeated display
            # summaries must also fit the bounded token's total JSON size.
            fault = ("; ".join(result.findings) or f"{result.stage.value}: {result.check_id} failed")[:512]
            changes.update(place=Place.READY, faults=token.faults + (fault,), blocker=fault,
                           next_action="Correct the validation fault and submit the task again",
                           evidence=tuple(replace(item, state=ResultState.STALE) if item != result else item for item in results))
        return replace(token, **changes)
    reason = event.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        _record_error("Workflow control requires a reason")
    for field in ("next_action", "responsible"):
        if field in event:
            if not event[field].strip():
                _record_error(f"Workflow control requires a nonempty {field}")
            changes[field] = event[field]
    if name in {"validation_failure", "integration_failure", "work_failure"}:
        changes.update(faults=token.faults + (reason,), blocker=reason, evidence=_stale_evidence(token),
                       next_action=event.get("next_action", "Correct the fault and reassess the task"))
    elif name in {"hold", "defer"}:
        changes.update(hold_reason=reason, blocker=reason,
                       interrupted_place=token.interrupted_place if token.pending_action else token.place,
                       next_action=event.get("next_action", "Resolve the control request"))
        if name == "defer":
            changes.update(_defer_trigger(token, event, context))
        else:
            changes.update(deferred_until=None, milestone_task_id=None)
        if context.get("effects_resolved") is not True:
            changes.update(place=token.place, pending_action=name,
                           next_action="Resolve the active effects before applying the control request")
        else:
            changes["pending_action"] = None
    elif name in {"release_hold", "resume_deferred", "reopen"}:
        changes.update(blocker=None, hold_reason=None, deferred_until=None, milestone_task_id=None,
                       interrupted_place=None, pending_action=None,
                       next_action=event.get("next_action", "Reassess inputs, dependencies and permissions before admission"))
        if name == "reopen":
            changes.update(input_generation=token.input_generation + 1, evidence=_stale_evidence(token))
    elif name == "update_control":
        changes.update(hold_reason=reason, blocker=reason)
        if "until" in event or "milestone_task_id" in event:
            if token.place != Place.DEFERRED:
                _record_error("Only Deferred accepts a deferral trigger")
            changes.update(_defer_trigger(token, event, context))
    return replace(token, **changes)


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
