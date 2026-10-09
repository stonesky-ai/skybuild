# Launch-free bootstrap implementation brief

Architecture A33 sections 3–7, 9 and 13 govern. On 2026-10-08 the owner authorized starting MVP implementation with Sol/Luna subprocesses while away and requested that questions accumulate rather than block progress. This supersedes the earlier planning-only session restriction for coding and isolated validation. It does not authorize production ledger cutover, live fleet starts, paid services, shared endpoint benchmarks or deployment.

The first slice implements SKYBUILD-BOOTSTRAP: a Python/FastAPI service, dedicated PostgreSQL Store, authenticated project-scoped tasks/history and minimal Cord. All source remains in SkyBuild. The ledgers remain authoritative until a separately validated cutover. There are no launch endpoints, model calls, timers or autonomous workers in this slice.

## Selected compact contracts

Use Python 3.12 or later, FastAPI/Pydantic 2, psycopg 3 synchronous transactions and explicit versioned SQL migrations. Keep synchronous handlers so database calls do not block the event loop. Store JSONB only for bounded task metadata; retain project/task IDs, revisions, dependencies and journal keys as relational fields. Use a dedicated database with an exact configured expected name; reject a mismatched actual database before migration or service readiness. Development validation uses only a newly created task-owned PostgreSQL container and database, never a discovered application DSN.

Package: `src/skybuild`. The Store owns all authorization and mutation transactions; API handlers validate and translate errors. `create_app(store)` accepts an initialized Store for testing. Runtime configuration is loaded only by an explicit factory/CLI, never on import. No import connects, migrates, starts a service or reads the existing inference credential.

Shared interfaces in `contracts.py`:

- `Principal(principal_id: str, is_admin: bool, grants: dict[str, frozenset[str]])`, a frozen dataclass.
- `DomainError(code: str, message: str, status_code: int = 400)` with public attributes.
- Operations: `tasks:read`, `tasks:write`, `cord:send`, `cord:read`, `cord:handle`. Admin bypasses project operation scopes; workers never gain admin rights through task fields.
- Store methods return JSON-compatible dictionaries/lists. All mutation methods enforce their operation scope internally, accept an authenticated Principal, and journal the server-derived actor.

Store constructor: `Store(dsn: str, expected_database: str)`. Methods:

```
readiness() -> dict
migrate() -> None
provision_principal(principal_id, token, *, is_admin=False, grants=None) -> None
authenticate(token: str) -> Principal
create_task(principal, project_id, body: dict, idempotency_key: str) -> dict
list_tasks(principal, project_id, *, limit=100, offset=0) -> list[dict]
get_task(principal, project_id, task_id) -> dict
update_task(principal, project_id, task_id, body: dict, expected_revision: int, idempotency_key: str) -> dict
task_history(principal, project_id, task_id, *, limit=100, offset=0) -> list[dict]
send_message(principal, project_id, body: dict, idempotency_key: str) -> dict
inbox(principal, project_id, *, limit=100, offset=0) -> list[dict]
message_action(principal, project_id, message_id, action: str, body: dict, idempotency_key: str) -> dict
```

`provision_principal` is a trusted offline administrative operation, never an unauthenticated or worker HTTP route. Store only a SHA-256 verifier of high-entropy tokens. Replacing a principal's credential preserves identity/history and revokes the previous token. Reject short/empty credentials. Production role separation and deployment credentials remain an adoption gate; a migration administrator is not the runtime database role.

Task creation requires `task_id`, `title`, `description`; supports `status` (proposed/ready/in-progress/blocked/deferred/done), `priority`, `dependencies`, `acceptance_criteria`, `architecture_refs`, `assignee`, `phase`, `next_action`, `blocker`, `responsible`, `metadata`. Defaults never authorize execution. Unfinished tasks require a next action or blocker and a responsible component/person; defaults may identify manual triage by owner. The task response includes project_id, task_id, revision, created_at and updated_at. Updates reject unknown/immutable fields. Dependencies refer to tasks in the same project and must remain acyclic; serialize dependency mutation per project in the transaction. Explicit project registration can remain a trusted administrative concern; project IDs are nonempty bounded strings and grants establish accessible projects.

Mutations use actor/project/operation-scoped idempotency keys plus a canonical payload hash. Same key and payload returns the original result; conflicting reuse returns 409. Task updates require expected revision and return 409 on stale revision. Task mutation and append-only history commit atomically. Protect journal UPDATE/DELETE at the database layer, not only by omitting API routes. Task deletion is not exposed.

Implemented validation bounds are: identifiers 200 characters, title/subject 500, description/message body 32,768, phase/responsible 200, next action/blocker 4,096, and at most 100 list entries. Identifiers preserve spaces and Unicode but reject slash, backslash, percent, Unicode control characters and the standalone names `.` and `..`. Metadata is limited to 16 KiB of canonical JSON, aggregate Store JSON to 64 KiB, HTTP bodies to 256 KiB and JSON nesting to 64 containers. Priority is a signed 32-bit integer. These are bootstrap input limits, not model/context budgets.

For this bounded bootstrap, each list request is an ordered bounded view, not a guaranteed snapshot across offset pages. Clients must not use offset pagination as a commit-order change feed. Cord inbox reads pending/unhandled messages for the authenticated recipient, repeatedly and idempotently; receipts do not remove unhandled messages. This avoids silently losing late commits. Enforce limits 1–100 and nonnegative offsets; cap request body size and text/metadata lengths.

Message creation requires `recipient`, `subject`, `body`; supports `category`, `urgency`, `task_id`, `reply_to`, `expires_at`. Sender comes from Principal. A recipient must exist and have project Cord access. Preserve accepted/delivered/handled/replied distinctions. `message_action` supports `receipt`, `handle`, `reply`; only the recipient or admin may act. Reply body includes subject/body and optional `handle_original`; reply insertion and requested original handling commit together. Replies link to the original and reverse sender/recipient; retry never duplicates them. A reply alone does not handle the original. Messages never invoke tools.

Cord changes also append immutable actor/action/state events in the same transaction; the retry result cache is not a substitute for history. The combined reply-and-handle operation records both distinctions, and replay adds no duplicate events. Migration 002 supplies this journal for installations of the initial 001 schema without inventing actor history for earlier messages. A receipt requires `cord:read`, handling requires `cord:handle`, and reply requires both `cord:handle` and `cord:send`. Supplying `reply_to` during message creation has the same reply authority and direction checks.

HTTP routes under `/api/v1/projects/{project_id}` mirror architecture section 5: `/tasks`, `/tasks/{task_id}`, `/tasks/{task_id}/history`, `/cord/messages`, `/cord/inbox`, `/cord/messages/{message_id}/{action}`. Use Bearer authentication, `Idempotency-Key` for writes and numeric `If-Match` for PATCH. Task POST returns 201, normal reads/PATCH/message actions 200; send returns 201. Error envelope is `{"error": {"code": ..., "message": ...}}`. Keep authentication failures generic and never expose DSNs/tokens in HTTP errors. Health routes `/health/live`, `/health/ready` plus `/version` and `/` contain no secrets. Readiness returns 503 when database/schema identity is unavailable. The public factory must not auto-migrate.

## Work ownership and validation

The lead owns this brief, contracts.py, packaging and integration tests/coordination. The Store worker owns store.py, migrations and focused Store tests. The API worker owns api.py, client.py, __main__.py and focused API/client tests. Shared-contract changes go through the lead. This is manually assigned bootstrap work, not runtime claims. Workers must not modify pre-existing planning files, `.env`, `dum.txt`, credentials or live infrastructure.

A separate Luna worker supplied the read-only ledger manifest helper and its tests. The CLI exposes it without database credentials. It preserves raw task sections and source hashes, but neither resolves prose dependencies nor imports or changes authority. The later thin workbench uses same-origin API requests and keeps its bearer token only in page memory; no cookies or browser storage are required. Its development against disposable tasks does not move live task authority.

Run syntax/import and applicable targeted tests first. Validate PostgreSQL behavior against a task-owned disposable target: wrong database refusal, restart persistence, auth/project isolation, stale updates, idempotency conflicts/replay, concurrent cycle attempts, immutable journal and transactional reply/handling. Use a separate Sol session for adversarial code review; corrections rerun relevant checks and independent re-review before integration acceptance. Do not declare the whole bootstrap complete until Tailscale/browser access and deployment-role checks are actually qualified. Unresolved owner questions go in the session handoff with their precise activation boundary.
