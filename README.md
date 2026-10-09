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
uv run python -m pytest
```

Use newly created, task-owned databases. These are example placeholders, not credentials or a command to reuse an application database. The HTTP integration suite requires a database name beginning with `skybuild_` and ending with `_test`.

## Explicit local commands

Set `SKYBUILD_DSN` and `SKYBUILD_EXPECTED_DATABASE` to the same dedicated database identity before administrative commands. The service never migrates on import or startup.

```sh
uv run skybuild migrate
uv run skybuild provision owner --admin --token-stdin
uv run skybuild serve --port 8000
```

Provisioning reads a high-entropy bearer token from standard input or `SKYBUILD_TOKEN`; it stores only a verifier. Use a separate token for each worker. Grant project-scoped operations with repeated `--grant PROJECT:OPERATION` arguments. The supported operations are `tasks:read`, `tasks:write`, `cord:send`, `cord:read`, and `cord:handle`. Provisioning replaces the principal's previous token and scopes; it is a trusted offline administration command, not a worker API.

`serve` binds to loopback. Tailscale exposure, browser-session handling and a restricted runtime database role require deployment qualification. Use a separate migration administrator; do not deploy the service using the disposable tests' PostgreSQL superuser.

Open `/workbench` on that local service for task list, creation, detail, edits and history. Enter the project and bearer token; the token stays only in page memory and is cleared on logout/reload. The page shows at most 100 tasks and 100 history events. A stale edit requires an explicit refresh before saving. Split/merge, automatic reassessment and live task cutover are not implemented yet.

Set `SKYBUILD_API_URL` and `SKYBUILD_TOKEN` for read-only CLI views:

```sh
uv run skybuild tasks PROJECT
uv run skybuild get PROJECT TASK
uv run skybuild history PROJECT TASK
```

Authenticated task and Cord routes live under `/api/v1/projects/{project_id}`. Writes require `Idempotency-Key`; task PATCH also requires a numeric `If-Match` revision. Identity fields remain immutable. `/health/live`, `/health/ready`, `/version`, and `/` expose no configuration secrets. Readiness distinguishes a live process from an available, correctly migrated database.

The read-only `skybuild ledger-manifest PATH...` command preserves task sections and source hashes for a future frozen import. It does not interpret prose dependencies, write the API, or perform a cutover. List offsets provide bounded views, not a stable change stream; Cord clients repeatedly scan unhandled messages and use idempotent receipts/actions.
