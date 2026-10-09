# SkyBuild

SkyBuild is being built toward a running system that rebuilds and extends itself with parallel workers. The current code is the launch-free bootstrap: a PostgreSQL task service, append-only task and Cord history, durable messages, a shared HTTP client, explicit administrative commands, and a thin browser task workbench. It does not launch workers or switch the Markdown ledgers to API authority.

The [architecture](docs/design/architecture.md) governs the [MVP sequence](docs/design/implementation_plan.md). The selected [bootstrap contract](docs/design/implementation/bootstrap.md) describes the current slice. [Session handoff](docs/design/session_handoff.md) records actual progress and remaining gates.

## Local development

Use Python 3.12 or later and `uv`:

```sh
uv sync --extra test
uv run python -m pytest
```

PostgreSQL tests require explicit disposable targets. Without these variables, database integration tests skip:

```sh
export SKYBUILD_TEST_DSN='postgresql://USER:PASSWORD@127.0.0.1:PORT/skybuild_test'
export SKYBUILD_HTTP_TEST_DSN='postgresql://USER:PASSWORD@127.0.0.1:PORT/skybuild_http_test'
export SKYBUILD_IMPORT_TEST_DSN='postgresql://USER:PASSWORD@127.0.0.1:PORT/skybuild_import_test'
uv run python -m pytest
```

Use newly created, task-owned databases. These are example placeholders, not credentials or a command to reuse an application database. The HTTP integration suite requires a database name beginning with `skybuild_` and ending with `_test`; the importer suite requires the exact `skybuild_import_test` name and creates isolated test databases from it.

## Explicit local commands

Set `SKYBUILD_DSN` and `SKYBUILD_EXPECTED_DATABASE` to the same dedicated database identity before administrative commands. The service never migrates on import or startup.

```sh
uv run skybuild migrate
uv run skybuild provision owner --admin --token-stdin
uv run skybuild serve --port 8000
```

Provisioning reads a high-entropy bearer token from standard input or `SKYBUILD_TOKEN`; it stores only a verifier. Use a separate token for each worker. Grant project-scoped operations with repeated `--grant PROJECT:OPERATION` arguments. The supported operations are `tasks:read`, `tasks:write`, `cord:send`, `cord:read`, and `cord:handle`. Provisioning replaces the principal's previous token and scopes; it is a trusted offline administration command, not a worker API.

`serve` binds to loopback. Tailscale exposure, browser-session handling and a restricted runtime database role require deployment qualification. Use a separate migration administrator; do not deploy the service using the disposable tests' PostgreSQL superuser.

Open `/workbench` on that local service for task creation, paged list/detail/history, definition edits, lineage and guarded actions. It previews proposed-only split/merge plans before applying them. Enter the project and bearer token; the token stays only in page memory and is cleared on logout/reload. A stale edit requires an explicit refresh before saving. Definition and dependency changes durably invalidate readiness; this does not admit or start a worker. Live task cutover is not implemented.

Set `SKYBUILD_API_URL` and `SKYBUILD_TOKEN` for read-only CLI views:

```sh
uv run skybuild tasks PROJECT
uv run skybuild get PROJECT TASK
uv run skybuild history PROJECT TASK
```

Authenticated task and Cord routes live under `/api/v1/projects/{project_id}`. Writes require `Idempotency-Key`; task PATCH also requires a numeric `If-Match` revision. Identity fields remain immutable. `/health/live`, `/health/ready`, `/version`, and `/` expose no configuration secrets. Readiness distinguishes a live process from an available, correctly migrated database.

`skybuild ledger-manifest PATH...` reads ledger source and hashes without database access. `skybuild ledger-import` dry-runs or rehearses the pinned frozen ledger and explicit dependency mapping against a disposable dedicated PostgreSQL database; it does not switch live authority. Imported SkyBuild tasks remain write-blocked while their receipt says Markdown is authoritative. Task lists use a stable task-ID cursor; Cord clients repeatedly scan unhandled messages and use idempotent receipts/actions.

Owner/admin completion attestation is available at `POST /api/v1/projects/{project_id}/tasks/{task_id}/complete`. It records exact acceptance, passing check, separate review and confirmed publication references. The endpoint trusts the owner's references; it does not independently verify GitHub or launch work. Current dependency acceptance and durable readiness generations guard dependent readiness and completion.

## Bounded due-deferral timer

The explicit CPU-only timer uses the authenticated API and never starts with `serve` or an import:

```sh
uv run skybuild schedule-due PROJECT --interval-seconds 60 --max-ticks 60
```

Configure `SKYBUILD_API_URL` and a project-scoped `SKYBUILD_TOKEN` with task read/write access. The first tick catches up immediately. Each tick processes at most `--max-pages` pages of `--page-size` tasks (defaults 20 and 100). An unfinished sweep continues at the next tick; a completed sweep starts again from the beginning. Delay begins after each tick, avoiding bursts after laptop downtime. The timer exits after the configured finite tick count. No operating-system timer is installed.

Each tick writes flushed JSON with confirmed progress and its continuation cursor. Exit 0 means the final sweep completed; exit 1 means an incomplete final sweep or an unconfirmed API page. Any API failure stops scheduling immediately. Restart safely begins a fresh sweep; guarded resume transitions avoid duplicate accepted transitions. Multiple callers use the existing revision checks, not independent authority. Markdown-owned imported projects remain write-blocked. The timer only requests reassessment; it never starts workers, calls models, admits work or switches authority.
