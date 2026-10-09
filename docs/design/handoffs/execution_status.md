# Read-only execution status handoff

Task: `SKYBUILD-EXECUTION-CONTROLS`. Owner: main SkyBuild session. Phase: awaiting independent review and the combined PostgreSQL gate. Branch: `task/execution-status`. Contract source: `3bfbbb0e65943c52932e3f8efd8764983501ddb8`. Wiring base: published TLS revision `7d40df9fa7b26035736ffa613b5c5dad548269f5`.

This first slice reads recorded state for exactly one required task. It adds `Store.execution_status`, `GET /api/v1/projects/{project_id}/tasks/{task_id}/execution-status`, `Client.execution_status`, and `skybuild execution-status PROJECT TASK [--limit N] [--ca-file PATH]`. The existing global `--ca-file` option also applies. It makes no probes or mutations, performs no reconciliation, starts nothing, releases nothing, and supplies no live cutover or deployment authority.

## Bounded response and sources

Architecture section 9 requires queryable cached status while keeping observed reports separate from execution authority. Implementation-plan area 5 starts with CLI inspection. The existing sources at the contract revision are `store.py:_connection`, `_authorize`, `_page`, `_task`, and `_public`; `claims.py:claim_history`; and `observations.py:observation_status`. Tables come only from migrations 006–009. The new method uses existing connection/authorization/page/public helpers, but selects task ID/revision explicitly rather than `_task`'s broader task payload.

The response contains task ID/revision; an optional recorded claim with fence, holder, task revision, lease time and held flag; and three independently bounded sections named `reservations`, `effects`, and `observations`. Each section contains `items` and an explicit `truncated` boolean. The shared limit defaults to 20 and is bounded to 1–100. Each SQL child query loads at most `limit + 1` rows, then trims the sentinel. Unique deterministic order uses reservation action ID, effect operation ID, and observation component/source ID respectively.

Reservation items expose only action/attempt IDs, actor, claim fence, task revision, units and recorded state. Effect items expose only operation/attempt IDs, task revision, recorded state, held exposure and creation time. Observations expose only event/attempt/component/source IDs, sequence, observation/receipt times and reported state from current cached projections. There is no raw journal, process identity, evidence reference, digest, allocation reference, arbitrary JSON, capacity, enabled flag, control generation, fake receipt, or derived overall safety/completion result.

Missing claim means no recorded claim. An empty child section means no records observed in that statement's snapshot; missing observations leave process state unknown. Neither empty nor truncated results establish process absence, task completion, or cleared exposure. A reported exit never overrides a held claim, reservation, or effect. Fake CPU tables and receipts are outside this first slice and require a later explicit extension.

## Authorization and coherent snapshot

The method rechecks current `tasks:read` project grants before reading task existence. Authorized missing tasks receive 404; unauthorized requests receive 403 for both existing and missing IDs. Authentication remains the existing API dependency. Every SQL section is constrained to both project and task, including each side of the observation projection/event join. The Store's existing database error boundary returns 503 rather than an empty-success fallback.

All returned task, claim, reservation, effect and observation fields come from one SQL statement after authorization. PostgreSQL therefore supplies one coherent MVCC data snapshot at normal READ COMMITTED isolation. The code does not force READ ONLY, which would conflict with the existing `lock_principal` row-lock helper. It adds no migration or runtime grants.

## Validation and next action

Focused command: `PYTHONPATH=src /home/kevin/my_code/skybuild/.venv/bin/python -m pytest -q tests/test_execution_status.py tests/test_client.py tests/test_client_tls.py`. Local result before commit: 60 passed, 6 skipped. Skips require parent-owned disposable PostgreSQL DSNs; no Docker or live controller data was used by this author.

Database tests cover grant revocation, tasks:claim-only denial, unauthorized existence concealment, authorized missing tasks, same task ID in another project, exact response allowlists, hostile hidden metadata/identities/digests, independent tasks, missing observations, default/max/invalid bounds, per-section truncation, restricted-runtime API access, unchanged rows/journals, and expired claims with reported exits retaining unresolved capacity. A deterministic interleaving test commits claim release and reservation cancellation atomically immediately after the first claim-bearing data query has materialized; the returned snapshot must remain old/old, with the following read new/new. This would expose a sequential READ COMMITTED implementation returning old claim/new reservation.

Non-database checks cover CLI/client read-only routing, bound/CA forwarding, invalid limits before database access, and safe 503 errors. Tests assert that `skybuild.__file__` belongs to this task checkout. Next: independent exact-head review, parent combined disposable PostgreSQL gate, fixes/re-review if needed, then frozen-bundle integration. Deployment remains a separate action.
