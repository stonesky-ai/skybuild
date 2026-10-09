"""Validated, launch-free HTTP interface to the transaction-owning Store."""

import json
import re
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, StrictInt, StringConstraints, field_validator
from starlette.exceptions import HTTPException

from . import __version__
from .contracts import DomainError, Principal, valid_identifier
from .web import install_workbench


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

    @app.post(base + "/tasks", status_code=201)
    def create_task(project_id: ProjectPath, body: TaskCreate, actor: Actor, idem: Key) -> dict:
        return store.create_task(actor, project_id, body.model_dump(mode="json", exclude_unset=True), idem)

    @app.get(base + "/tasks")
    def list_tasks(project_id: ProjectPath, actor: Actor, limit: Limit = 100, offset: Offset = 0,
                   after_task_id: Identifier | None = None, by_id: bool = False) -> list:
        return store.list_tasks(actor, project_id, limit=limit, offset=offset, after_task_id=after_task_id, by_id=by_id)

    @app.get(base + "/tasks/{task_id}")
    def get_task(project_id: ProjectPath, task_id: RecordPath, actor: Actor) -> dict:
        return store.get_task(actor, project_id, task_id)

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
                                limit: Limit = 100, offset: Offset = 0) -> dict:
        return store.reconcile_due_deferrals(actor, project_id, idem, limit=limit, offset=offset)

    @app.post(base + "/cord/messages", status_code=201)
    def send_message(project_id: ProjectPath, body: MessageCreate, actor: Actor, idem: Key) -> dict:
        return store.send_message(actor, project_id, body.model_dump(mode="json", exclude_unset=True), idem)

    @app.get(base + "/cord/inbox")
    def inbox(project_id: ProjectPath, actor: Actor, limit: Limit = 100, offset: Offset = 0) -> list:
        return store.inbox(actor, project_id, limit=limit, offset=offset)

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
