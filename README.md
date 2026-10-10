# SkyBuild

CPU reservation scaffold (Store only): `configure_cpu_pool` and
`set_cpu_local_control` persist separate owner/admin restrictions with expected
generations. A newly configured project pool starts locally disabled. Both controls
must be enabled at their current generations for `reserve_cpu`. Reservations bind
global immutable action and attempt identities, the authenticated actor, task
revision, assessed readiness generation and current claim fence. CPU units are
positive integers in a finite, project-scoped pool; capacity cannot shrink below
held units. Replaying an action returns its current state, including cancellation.

These reservations are **unredeemable** and authorize no execution. There is no
launcher, dispatch, redemption, HTTP route, model/provider/billing accounting or
full architecture section 8 admission guarantee. Fleet, box, role and run controls,
runtime authority and physical launch reconciliation remain unimplemented; future
physical launch must remain denied until every applicable control is enforced.
No migration, authority switch, worker start or deployment is implicit.

Only explicit `cancel_cpu_reservation` releases units, and only where no effect
history exists for the task. Current claim holders may cancel; owner/admin may
cancel a never-dispatched reservation after lease expiry. Cancellation does not
release the claim. Stop, timeout, lost contact, claim expiry, claim reconciliation
and unknown effect observations never free CPU units. Held reservations block
claim release/reconciliation and semantic task mutation. Control and reservation
events share an append-only `cpu_journal` transaction with their state changes.

SkyBuild is being built toward a running system that rebuilds and extends itself with parallel workers. The current code is the launch-free bootstrap: a PostgreSQL task service, append-only task and Cord history, durable messages, a shared HTTP client, explicit administrative commands, and a thin browser task workbench. The deployed manual pilot serves the imported task ledger through the authenticated REST API and does not launch workers or models.

The [architecture](docs/design/architecture.md) governs the [MVP sequence](docs/design/implementation_plan.md). The selected [bootstrap contract](docs/design/implementation/bootstrap.md) describes the current slice. [Session handoff](docs/design/session_handoff.md) records actual progress and remaining gates.
At cycle closeout, merge the current `dev-NNN` branch to `main`, start the next sequential development branch with a one-line README commit, and open its standing pull request.

## Local development

Use Python 3.12 or later and `uv`:

```sh
scripts/project_python -m pytest
```

`scripts/project_python` runs Python through `uv` with this checkout's locked
runtime and test dependencies, its own `.venv`, and its absolute `src` and
`scripts` paths. Use
it for repository scripts too, including help:

```sh
scripts/project_python scripts/marshall_bundle.py --help
```

The launcher preserves your working directory and Python arguments. From another
directory, use absolute launcher and script/test paths. It replaces inherited
`PYTHONPATH` and `UV_PROJECT_ENVIRONMENT`, so another checkout's editable install
cannot select the package for this command. Dependency synchronization failure
stops the invocation; no fallback to system Python occurs. The launcher requires
`uv`, a POSIX shell, and the committed `pyproject.toml` and `uv.lock`. It does not
start a service unless the Python command you explicitly supply does so.
Its cache defaults to ignored `.uv-cache/` inside the checkout, which also works
when the home directory is read-only. An explicit `UV_CACHE_DIR` remains supported
for a writable shared cache.
Python's `-P` mode suppresses implicit caller/script-directory imports; repository
helpers resolve through the explicit `scripts` path. External scripts that depend
on sibling imports need their own invocation instead. Python options such as
`-E` or `-I` deliberately bypass `PYTHONPATH`; do not use them for source checks.
The launcher clears inherited `UV_WORKING_DIR`, which could otherwise change the
directory before Python resolves a relative script path.
Reviewed integration and disposable PostgreSQL gates rebind the environment and
source paths to their own candidate before running nested Python commands. The
author checkout's launcher selection must not leak into candidate validation.

PostgreSQL tests require explicit disposable targets. Without these variables, database integration tests skip:

```sh
export SKYBUILD_TEST_DSN='postgresql://USER:PASSWORD@127.0.0.1:PORT/skybuild_test'
export SKYBUILD_HTTP_TEST_DSN='postgresql://USER:PASSWORD@127.0.0.1:PORT/skybuild_http_test'
export SKYBUILD_IMPORT_TEST_DSN='postgresql://USER:PASSWORD@127.0.0.1:PORT/skybuild_import_test'
scripts/project_python -m pytest
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

`serve` binds to loopback. Tailscale exposure, browser-session handling and a restricted runtime database role require deployment qualification. Use a separate migration administrator; do not deploy the service using the disposable tests' PostgreSQL superuser. For the manual-worker pilot, `/api/v1/me` reports only the authenticated caller's identity and grants; `python -m skybuild.fleet_preflight` checks private HTTPS readiness and narrow Cord access from a worker using a mode-0600 token file. It is read-only and does not replace a Cord round trip.

Open `/workbench` on the pilot service for task creation, paged list/detail/history, definition edits, lineage and guarded actions. It previews proposed-only split/merge plans before applying them. Enter the project and bearer token; the token stays only in page memory and is cleared on logout/reload. A stale edit requires an explicit refresh before saving. Definition and dependency changes durably invalidate readiness; this does not admit or start a worker. As verified on 2026-10-09, migration 012 and the guarded cutover are deployed: the authenticated API serves 60 tasks, including 38 imported from the frozen manifest and 22 created afterward. The three former ledger paths are retirement notices. The current manual pilot does not launch or reserve workers or models.

Set `SKYBUILD_API_URL` and `SKYBUILD_TOKEN` for read-only CLI views:

```sh
uv run skybuild tasks PROJECT
uv run skybuild get PROJECT TASK
uv run skybuild history PROJECT TASK
```

Authenticated task and Cord routes live under `/api/v1/projects/{project_id}`. Writes require `Idempotency-Key`; task PATCH also requires a numeric `If-Match` revision. Identity fields remain immutable. `/health/live`, `/health/ready`, `/version`, and `/` expose no configuration secrets. Readiness distinguishes a live process from an available, correctly migrated database.

`skybuild ledger-manifest PATH...` reads ledger source and hashes without database access. `skybuild ledger-import` dry-runs or rehearses a pinned frozen ledger and explicit dependency mapping against a disposable dedicated PostgreSQL database; it does not switch live authority. `skybuild ledger-audit` compares a selected frozen source with current Markdown, uses no database, and exits 2 when the source differs. The committed historical contract reports 28 frozen tasks; the accepted cutover contract `docs/design/implementation/current_task_import.json` pins revision A39 at 38 tasks with an explicit dependency/workflow mapping. The live pilot is on schema 12 with an API-authority receipt and serves those 38 tasks. The former ledger paths now contain retirement notices; do not re-import them. Recover the frozen source from its pinned commit and use the verified backup/recovery procedure. Migration 012 blocks task writes and claims unless an explicit API-authority receipt exists. The API and workbench use a stable task-ID cursor for task lists; Cord clients repeatedly scan unhandled messages and use idempotent receipts/actions.

Owner/admin completion attestation is available at `POST /api/v1/projects/{project_id}/tasks/{task_id}/complete`. It records exact acceptance, passing check, separate review and confirmed publication references. The endpoint trusts the owner's references; it does not independently verify GitHub or launch work. Current dependency acceptance and durable readiness generations guard dependent readiness and completion.

The Store also records durable effect intents and holds unknown exposure. This is launch-free storage with no public dispatch API or qualified terminal reconciliation. Task edits and structural actions remain conservatively blocked while an effect is unresolved.

## Bounded due-deferral timer

The explicit CPU-only timer uses the authenticated API and never starts with `serve` or an import:

```sh
uv run skybuild schedule-due PROJECT --interval-seconds 60 --max-ticks 60
```

Configure `SKYBUILD_API_URL` and a project-scoped `SKYBUILD_TOKEN` with task read/write access. The first tick catches up immediately. Each tick processes at most `--max-pages` pages of `--page-size` tasks (defaults 20 and 100). An unfinished sweep continues at the next tick; a completed sweep starts again from the beginning. Delay begins after each tick, avoiding bursts after laptop downtime. The timer exits after the configured finite tick count. No operating-system timer is installed.

Each tick writes flushed JSON with confirmed progress and its continuation cursor. Exit 0 means the final sweep completed; exit 1 means an incomplete final sweep or an unconfirmed API page. Any API failure stops scheduling immediately. Restart safely begins a fresh sweep; guarded resume transitions avoid duplicate accepted transitions. Multiple callers use the existing revision checks, not independent authority. Markdown-owned imported projects remain write-blocked. The timer only requests reassessment; it never starts workers, calls models, admits work or switches authority.

Restricted service-role provisioning, audit, and disposable qualification are documented in [runtime role qualification](docs/design/implementation/runtime_role.md). These tools do not authorize live changes.


For the explicitly selected local Dunsel development preview, run `uv run uvicorn skybuild.workbench_preview:app --host 127.0.0.1 --port 8766` and open `/workbench/marshalls`. The preview exposes explicit controls for one fixed CPU-only memory/disk sampler. Starting the preview does not start Dunsel. The normal service does not register these routes. Keep the preview bound to loopback; mutation requests also require the same browser origin. No task API, database, model, or credential is used by Dunsel.

Dunsel state lives in the private `~/.local/state/skybuild/dunsel/` directory, with mode 0700 and owned regular files of mode 0600. The log is `dunsel.log`; graceful stop uses `dunsel.off-now`. Symlinks, hardlinks, and non-regular state files are refused. The shared control lock and kernel-validated PID/start-time/argument record allow previews from different checkouts to control the same worker without starting duplicates. Disable only blocks later starts. Graceful stop consumes its request at the next sample; Kill uses the exact recorded worker command.

A durable pre-launch intent blocks retries when a start has unknown effects, including failures to publish the child identity. If the preview reports an unresolved startup without a readable process record, reconcile the physical process before clearing state; restarting another checkout does not clear that barrier.
Development cycle dev-004 starts from main commit 50e6dfb4c622114f97b348bf679e8a45191aa2a4.
Development cycle dev-005 started on 2026-10-09T22:31:06-05:00.
