"""Validated, launch-free HTTP interface to the transaction-owning Store."""

import asyncio
import json
import re
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, StrictInt, StringConstraints, field_validator, model_validator
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from . import __version__
from .contracts import DomainError, Principal, valid_identifier
from .web import install_workbench
from .workflow import TRANSITIONS, _workflow_event


def _identifier(value: str) -> str:
    if not valid_identifier(value):
        raise ValueError("Identifier is not addressable")
    return value

MAX_BODY_BYTES = 262_144
MAX_JSON_DEPTH = 64
Identifier = Annotated[str, StringConstraints(min_length=1, max_length=200), AfterValidator(_identifier)]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
LongText = Annotated[str, StringConstraints(min_length=1, max_length=32_768)]
WorkflowText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
OptionalText = Annotated[str, StringConstraints(max_length=4096)]
Status = Literal["proposed", "ready", "in-progress", "blocked", "deferred", "done", "superseded"]
IdList = Annotated[list[Identifier], Field(max_length=100)]
TextList = Annotated[list[Annotated[str, StringConstraints(min_length=1, max_length=4096)]], Field(max_length=100)]
ProjectPath = Annotated[Identifier, Path()]
RecordPath = Annotated[Identifier, Path()]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskFields(Input):
    title: ShortText | None = None
    description: LongText | None = None
    status: Status | None = None
    priority: Annotated[StrictInt, Field(ge=-(2**31), lt=2**31)] | None = None
    dependencies: IdList | None = None
    acceptance_criteria: TextList | None = None
    architecture_refs: TextList | None = None
    assignee: Identifier | None = None
    phase: WorkflowText | None = None
    next_action: OptionalText | None = None
    blocker: OptionalText | None = None
    responsible: WorkflowText | None = None
    metadata: dict[str, Any] | None = None

    @field_validator("metadata")
    @classmethod
    def bounded_metadata(cls, value: dict | None) -> dict | None:
        if value is not None:
            if set(value) & {"_skybuild_workflow", "_skybuild_completion"}:
                raise ValueError("Workflow metadata is managed by task actions")
            try:
                encoded = json.dumps(value, allow_nan=False, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            except (ValueError, RecursionError) as error:
                raise ValueError("metadata must be finite JSON") from error
            if len(encoded) > 16_384:
                raise ValueError("metadata is too large")
        return value

    @field_validator("title", "description", "status", "priority", "dependencies", "acceptance_criteria", "architecture_refs", "phase", "responsible", "metadata")
    @classmethod
    def reject_null(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("field cannot be null")
        return value


class TaskCreate(TaskFields):
    task_id: Identifier
    title: ShortText
    description: LongText


class TaskAction(Input):
    reason: OptionalText
    next_action: OptionalText | None = None
    responsible: WorkflowText | None = None
    until: AwareDatetime | None = None
    milestone_task_id: Identifier | None = None


class SubmissionReceipt(Input):
    """Author output identity. This receipt does not attest validation or acceptance."""

    source_head: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")]
    target_base: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")]
    source_branch: Annotated[str, StringConstraints(min_length=12, max_length=200)]
    attempt_id: Identifier
    claim_fence: Annotated[StrictInt, Field(ge=1, lt=2**63)]
    input_generation: Annotated[StrictInt, Field(ge=0, lt=2**63)]
    definition_revision: Annotated[StrictInt, Field(ge=0, lt=2**63)]
    policy_version: Annotated[str, StringConstraints(max_length=200)]

    @field_validator("source_branch")
    @classmethod
    def branch_reference(cls, value):
        if (not value.startswith("refs/heads/") or ".." in value or "@{" in value or
                any(ord(char) <= 32 or ord(char) == 127 or char in "~^:?*[\\" for char in value) or
                any(not part or part.startswith(".") or part.endswith((".", ".lock")) for part in value.split("/"))):
            raise ValueError("Submission requires a full branch reference")
        return value

    @field_validator("policy_version")
    @classmethod
    def policy_text(cls, value):
        if "\x00" in value:
            raise ValueError("Policy version cannot contain NUL")
        return value


class WorkflowTransition(Input):
    """Caller details only. Headers supply revision and operation identity."""

    event: Identifier
    reason: OptionalText | None = None
    next_action: OptionalText | None = None
    responsible: WorkflowText | None = None
    until: AwareDatetime | None = None
    milestone_task_id: Identifier | None = None
    result: dict[str, Any] | None = None
    source_head: str | None = None
    target_base: str | None = None
    source_branch: str | None = None
    attempt_id: str | None = None
    claim_fence: StrictInt | None = None
    input_generation: StrictInt | None = None
    definition_revision: StrictInt | None = None
    policy_version: str | None = None

    @model_validator(mode="after")
    def bounded_event(self):
        body = self.model_dump(mode="json", exclude_unset=True)
        if self.event not in {spec.event for spec in TRANSITIONS} | {"initialize"}:
            raise ValueError("Unknown workflow event")
        details = set(body) - {"event"}
        receipt_fields = set(SubmissionReceipt.model_fields)
        if self.event == "submit":
            if details != receipt_fields:
                raise ValueError("Submission requires the complete output receipt only")
            SubmissionReceipt.model_validate({name: body[name] for name in receipt_fields})
            try:
                json.dumps(body, allow_nan=False, ensure_ascii=False).encode()
            except (ValueError, UnicodeError):
                raise ValueError("Submission receipt must be finite UTF-8 JSON") from None
            return self
        if details & receipt_fields:
            raise ValueError("Only submission accepts an output receipt")
        _workflow_event({**body, "operation_id": "validate", "expected_revision": 0})
        if self.event in {"initialize", "claim", "freeze", "accept"}:
            if details:
                raise ValueError("This event does not accept caller details")
        elif self.event == "validation_result":
            if details != {"result"}:
                raise ValueError("Validation progress requires only a result")
        else:
            if "result" in details or not isinstance(self.reason, str) or not self.reason.strip():
                raise ValueError("Control events require a reason")
            if self.event not in {"defer", "update_control"} and details & {"until", "milestone_task_id"}:
                raise ValueError("This event does not accept a deferral trigger")
            if self.event == "defer" and (("until" in details) == ("milestone_task_id" in details)):
                raise ValueError("Deferral requires exactly one trigger")
        return self


class CPULocalControl(Input):
    enabled: StrictBool
    expected_generation: Annotated[StrictInt, Field(ge=0, lt=2**63)]
    reason: Annotated[str, StringConstraints(min_length=1, max_length=4096)]


class CPUCentralControl(CPULocalControl):
    capacity: Annotated[StrictInt, Field(ge=0, lt=2**31)]


class ClaimLease(Input):
    lease_seconds: Annotated[StrictInt, Field(ge=1, le=300)] = 60


class ClaimRenew(ClaimLease):
    fence: Annotated[StrictInt, Field(ge=1, lt=2**63)]


class ClaimRelease(Input):
    fence: Annotated[StrictInt, Field(ge=1, lt=2**63)]
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4096)]


class SplitChild(Input):
    task_id: Identifier
    title: ShortText
    description: LongText
    acceptance_criteria: TextList
    architecture_refs: TextList | None = None
    dependencies: IdList
    responsible: WorkflowText | None = None
    next_action: OptionalText | None = None


class TaskSplit(Input):
    reason: OptionalText
    children: Annotated[list[SplitChild], Field(min_length=2, max_length=10)]
    incoming: dict[str, IdList]


class TaskMerge(Input):
    reason: OptionalText
    source_task_ids: Annotated[list[Identifier], Field(min_length=2, max_length=10)]
    target: SplitChild
    incoming_dependents: IdList
    expected_revisions: dict[str, StrictInt]


class MessageCreate(Input):
    recipient: Identifier
    subject: ShortText
    body: LongText
    category: Annotated[str, StringConstraints(min_length=1, max_length=100)] = "misc"
    urgency: Annotated[str, StringConstraints(min_length=1, max_length=100)] = "normal"
    task_id: Identifier | None = None
    reply_to: Identifier | None = None
    expires_at: AwareDatetime | None = None


class MessageReply(Input):
    subject: ShortText
    body: LongText
    handle_original: StrictBool = False


def error_response(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def _excessive_json_depth(body: bytes) -> bool:
    """Bound container nesting before JSON decoding, ignoring quoted text."""
    depth, quoted, escaped = 0, False, False
    for byte in body:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
        elif byte in (91, 123):
            depth += 1
            if depth > MAX_JSON_DEPTH:
                return True
        elif byte in (93, 125):
            depth -= 1
    return False


class BodyLimit:
    """Buffer only a bounded request, including requests without Content-Length."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                return
            size += len(message.get("body", b""))
            if size > MAX_BODY_BYTES:
                await error_response("payload_too_large", "Request body exceeds the size limit", 413)(scope, receive, send)
                return
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        if _excessive_json_depth(body):
            await error_response("validation", "Invalid request", 422)(scope, receive, send)
            return
        delivered = False

        async def bounded_receive() -> dict:
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, bounded_receive, send)


def create_app(store: Any) -> FastAPI:
    """Create an app without connecting, migrating, or reading configuration."""
    app = FastAPI(title="SkyBuild", version=__version__)
    app.add_middleware(BodyLimit)

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, error: DomainError) -> JSONResponse:
        if error.status_code == 401:
            return error_response("unauthenticated", "Authentication required", 401)
        if error.status_code >= 500:
            return error_response("unavailable", "Service unavailable", error.status_code)
        return error_response(error.code, error.message, error.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
        return error_response("validation", "Invalid request", 422)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException) -> JSONResponse:
        return error_response("http_error", "Request failed", error.status_code)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, error: Exception) -> JSONResponse:
        return error_response("unavailable", "Service unavailable", 503)

    def principal(authorization: Annotated[str | None, Header()] = None) -> Principal:
        if not authorization or len(authorization) > 4096:
            raise DomainError("unauthenticated", "Authentication required", 401)
        parts = authorization.split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise DomainError("unauthenticated", "Authentication required", 401)
        return store.authenticate(parts[1])

    def key(idempotency_key: Annotated[str | None, Header()] = None) -> str:
        if not idempotency_key or not re.fullmatch(r"[\x21-\x7e]{1,128}", idempotency_key):
            raise DomainError("validation", "A bounded Idempotency-Key is required", 422)
        return idempotency_key

    def revision(if_match: Annotated[str | None, Header()] = None) -> int:
        if not if_match or not re.fullmatch(r"[0-9]{1,19}", if_match):
            raise DomainError("validation", "A numeric If-Match revision is required", 422)
        value = int(if_match)
        if value < 1 or value > 9_223_372_036_854_775_807:
            raise DomainError("validation", "Invalid If-Match revision", 422)
        return value

    Actor = Annotated[Principal, Depends(principal)]
    Key = Annotated[str, Depends(key)]
    Revision = Annotated[int, Depends(revision)]
    Limit = Annotated[int, Query(ge=1, le=100)]
    Offset = Annotated[int, Query(ge=0, le=1_000_000_000)]
    base = "/api/v1/projects/{project_id}"

    @app.get("/")
    def root() -> dict:
        return {"service": "skybuild", "message": "Hello from SkyBuild"}

    @app.get("/version")
    def version() -> dict:
        return {"service": "skybuild", "version": __version__, "protocol": "v1"}

    @app.get("/health/live")
    def live() -> dict:
        return {"status": "alive"}

    @app.get("/health/ready")
    def ready() -> JSONResponse:
        try:
            result = store.readiness()
            available = result.get("ready") is True or result.get("status") == "ready"
        except Exception:
            available = False
        return JSONResponse({"status": "ready" if available else "unavailable"}, status_code=200 if available else 503)

    @app.get("/api/v1/me")
    def me(actor: Actor) -> dict:
        """Expose only the caller's identity and grants for scoped access checks."""
        return {"principal_id": actor.principal_id, "is_admin": actor.is_admin,
                "grants": {project: sorted(operations) for project, operations in actor.grants.items()}}

    @app.get(base + "/cpu-controls")
    def cpu_control_status(project_id: ProjectPath, actor: Actor) -> dict:
        return store.cpu_control_status(actor, project_id)

    @app.post(base + "/cpu-controls/central")
    def configure_cpu_pool(project_id: ProjectPath, body: CPUCentralControl, actor: Actor, idem: Key) -> dict:
        return store.configure_cpu_pool(actor, project_id, body.capacity, body.enabled,
                                        body.expected_generation, idem, reason=body.reason)

    @app.post(base + "/cpu-controls/local")
    def set_cpu_local_control(project_id: ProjectPath, body: CPULocalControl, actor: Actor, idem: Key) -> dict:
        return store.set_cpu_local_control(actor, project_id, body.enabled,
                                           body.expected_generation, idem, reason=body.reason)

    @app.post(base + "/tasks", status_code=201)
    def create_task(project_id: ProjectPath, body: TaskCreate, actor: Actor, idem: Key) -> dict:
        return store.create_task(actor, project_id, body.model_dump(mode="json", exclude_unset=True), idem)

    @app.get(base + "/tasks")
    def list_tasks(project_id: ProjectPath, actor: Actor, limit: Limit = 100, offset: Offset = 0,
                   after_task_id: Identifier | None = None, by_id: bool = False) -> list:
        from .store import Store
        tasks = store.list_tasks(actor, project_id, limit=limit, offset=offset, after_task_id=after_task_id, by_id=by_id)
        return [{**task, **Store.workflow_projection(task)} for task in tasks]

    @app.get(base + "/tasks/{task_id}")
    def get_task(project_id: ProjectPath, task_id: RecordPath, actor: Actor) -> dict:
        from .store import Store
        task = store.get_task(actor, project_id, task_id)
        return {**task, **Store.workflow_projection(task)}

    @app.get(base + "/tasks/{task_id}/workflow")
    def task_workflow(project_id: ProjectPath, task_id: RecordPath, actor: Actor) -> dict:
        return store.task_workflow(actor, project_id, task_id)

    @app.post(base + "/tasks/{task_id}/workflow")
    def workflow_transition(project_id: ProjectPath, task_id: RecordPath, body: WorkflowTransition,
                            actor: Actor, idem: Key, expected: Revision) -> dict:
        if not valid_identifier(idem):
            raise DomainError("validation", "Workflow operation key must be addressable", 422)
        details = body.model_dump(mode="json", exclude_unset=True)
        event = details.pop("event")
        if event == "initialize":
            return store.initialize_workflow(actor, project_id, task_id, expected, idem)
        return store.workflow_transition(actor, project_id, task_id, event, details, expected, idem)

    @app.get(base + "/tasks/{task_id}/execution-status")
    def execution_status(project_id: ProjectPath, task_id: RecordPath, actor: Actor,
                         limit: Limit = 20) -> dict:
        return store.execution_status(actor, project_id, task_id, limit=limit)

    @app.patch(base + "/tasks/{task_id}")
    def update_task(project_id: ProjectPath, task_id: RecordPath, body: TaskFields, actor: Actor, idem: Key, expected: Revision) -> dict:
        return store.update_task(actor, project_id, task_id, body.model_dump(mode="json", exclude_unset=True), expected, idem)

    @app.get(base + "/tasks/{task_id}/history")
    def history(project_id: ProjectPath, task_id: RecordPath, actor: Actor, limit: Limit = 100, offset: Offset = 0) -> list:
        return store.task_history(actor, project_id, task_id, limit=limit, offset=offset)

    @app.post(base + "/tasks/{task_id}/actions/{action}")
    def task_action(project_id: ProjectPath, task_id: RecordPath, action: RecordPath, body: TaskAction,
                    actor: Actor, idem: Key, expected: Revision) -> dict:
        return store.task_action(actor, project_id, task_id, action,
                                 body.model_dump(mode="json", exclude_unset=True), expected, idem)

    @app.post(base + "/tasks/{task_id}/complete")
    def complete_task(project_id: ProjectPath, task_id: RecordPath, body: dict[str, Any],
                      actor: Actor, idem: Key, expected: Revision) -> dict:
        return store.complete_task(actor, project_id, task_id, body, expected, idem)

    @app.post(base + "/tasks/{task_id}/claim")
    def claim_task(project_id: ProjectPath, task_id: RecordPath, body: ClaimLease,
                   actor: Actor, idem: Key, expected: Revision) -> dict:
        return store.claim_task(actor, project_id, task_id, expected, idem, lease_seconds=body.lease_seconds)

    @app.post(base + "/tasks/{task_id}/claim/renew")
    def renew_claim(project_id: ProjectPath, task_id: RecordPath, body: ClaimRenew,
                    actor: Actor, idem: Key, expected: Revision) -> dict:
        return store.renew_claim(actor, project_id, task_id, body.fence, expected, idem, lease_seconds=body.lease_seconds)

    @app.post(base + "/tasks/{task_id}/claim/release")
    def release_claim(project_id: ProjectPath, task_id: RecordPath, body: ClaimRelease,
                      actor: Actor, idem: Key, expected: Revision) -> dict:
        return store.release_claim(actor, project_id, task_id, body.fence, expected, idem, reason=body.reason)

    @app.post(base + "/tasks/{task_id}/claim/reconcile")
    def reconcile_claim(project_id: ProjectPath, task_id: RecordPath, body: ClaimRelease,
                        actor: Actor, idem: Key, expected: Revision) -> dict:
        return store.reconcile_claim(actor, project_id, task_id, body.fence, expected, idem, reason=body.reason)

    @app.get(base + "/tasks/{task_id}/claim/history")
    def claim_history(project_id: ProjectPath, task_id: RecordPath, actor: Actor,
                      limit: Limit = 100, offset: Offset = 0) -> list:
        return store.claim_history(actor, project_id, task_id, limit=limit, offset=offset)

    @app.post(base + "/tasks/{task_id}/split")
    def split_task(project_id: ProjectPath, task_id: RecordPath, body: TaskSplit,
                   actor: Actor, idem: Key, expected: Revision) -> dict:
        return store.split_task(actor, project_id, task_id,
                                [child.model_dump(mode="json", exclude_unset=True) for child in body.children],
                                body.incoming, body.reason, expected, idem)

    @app.post(base + "/tasks/merge")
    def merge_tasks(project_id: ProjectPath, body: TaskMerge, actor: Actor, idem: Key) -> dict:
        return store.merge_tasks(actor, project_id, body.source_task_ids,
                                 body.target.model_dump(mode="json", exclude_unset=True),
                                 body.incoming_dependents, body.expected_revisions, body.reason, idem)

    @app.get(base + "/tasks/{task_id}/lineage")
    def task_lineage(project_id: ProjectPath, task_id: RecordPath, actor: Actor) -> list[dict]:
        return store.task_lineage(actor, project_id, task_id)

    @app.post(base + "/tasks/reconcile-due")
    def reconcile_due_deferrals(project_id: ProjectPath, actor: Actor, idem: Key,
                                limit: Limit = 100, after_task_id: Identifier | None = None) -> dict:
        return store.reconcile_due_deferrals(actor, project_id, idem, limit=limit, after_task_id=after_task_id)

    @app.post(base + "/cord/messages", status_code=201)
    def send_message(project_id: ProjectPath, body: MessageCreate, actor: Actor, idem: Key) -> dict:
        return store.send_message(actor, project_id, body.model_dump(mode="json", exclude_unset=True), idem)

    @app.get(base + "/cord/inbox")
    async def inbox(project_id: ProjectPath, actor: Actor, request: Request, limit: Limit = 100,
                    offset: Offset = 0, wait_seconds: Annotated[int, Query(ge=0, le=25)] = 0) -> list:
        """Wait for a pending page without claiming, receiving, or handling messages."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_seconds
        while True:
            messages = await run_in_threadpool(store.inbox, actor, project_id, limit=limit, offset=offset)
            remaining = deadline - loop.time()
            if messages or remaining <= 0 or await request.is_disconnected():
                return messages
            await asyncio.sleep(min(0.5, remaining))
            # Token rotation and grant revocation also apply to outstanding waits.
            if await request.is_disconnected():
                return []
            actor = await run_in_threadpool(principal, request.headers.get("authorization"))

    @app.post(base + "/cord/messages/{message_id}/receipt")
    def receipt(project_id: ProjectPath, message_id: RecordPath, body: Input, actor: Actor, idem: Key) -> dict:
        return store.message_action(actor, project_id, message_id, "receipt", body.model_dump(), idem)

    @app.post(base + "/cord/messages/{message_id}/handle")
    def handle(project_id: ProjectPath, message_id: RecordPath, body: Input, actor: Actor, idem: Key) -> dict:
        return store.message_action(actor, project_id, message_id, "handle", body.model_dump(), idem)

    @app.post(base + "/cord/messages/{message_id}/reply")
    def reply(project_id: ProjectPath, message_id: RecordPath, body: MessageReply, actor: Actor, idem: Key) -> dict:
        return store.message_action(actor, project_id, message_id, "reply", body.model_dump(), idem)

    install_workbench(app)
    return app
