# Skymaker migration inventory

Read-only review, 2026-10-08. No service, DB, agent, or transcript was started or changed. No `.env*` file was read. Main checkout is stale for the service: CodeGraph status reported `/home/kevin/my_code/skykeep` at schema v2 with 190 pending additions; `/home/kevin/my_code/skykeep-todo-live` has no index and is the relevant deployed-baseline checkout. Its HEAD is `1c314af98` (`TODO-SERVICE-TOOLING-PATHS-COVER-DOCS-DEV`). The architecture review cites this checkout as schema v19; this is a source baseline, not proof of the currently running SHA or database state.

## Existing build REST API and persistence

The service is `scripts/todo_service/app.py`, FastAPI over `scripts/todo_service/store.py`; it runs as `skykeep-todo-service.service` on jeltz, configured outside the repo, port 8888. It is shared by keeper/build boxes. `TodoClient` is the common HTTP client; Python admin/import/integration/scan/shell-worker paths also call `Store` or mutate its SQLite DB directly. Other active coordination components consume `/queue`, `/premerge`, `/integration`, `/gate`, `/items`, `/seams`, `/fleet`, `/batch-history`, `/integrator/*`, `/lessons`, `/alarms`, and `/boxes/*`. Preserve the existing route and client contracts to limit caller migration.

The v19 SQLite schema has 27 tables, not just the Cord mailbox:

- Work ledger: `schema_version`, `items`, `item_history`, `claims`, `seams`, `events`.
- Review/integration state: `seam_scan`, `integration_sets`, `priority_overrides`, `alarms`, `item_marks`, `set_marks`, `reviews`, `seam_state`, `seam_fact`.
- Fleet and configuration state: `component_events`, `component_status`, `box_settings`.
- Batch/gate state: `batch_run`, `batch_step`, `gate_runs`, `gate_jobs`, `gate_results`.
- Operational memory and mailbox: `lessons`, `integrator_lease`, `integrator_messages`, `integrator_message_handled`.

`GET /dump` exports a subset (items/history/marks/claims/seams, priority overrides, alarms, integration sets/marks, reviews, lessons). It omits many tables, including events, seam scans/facts/state, component state, batch/gate records, lease, and messages. It cannot be the full transfer source. The SQLite DB is WAL-backed; writes use per-operation connections and `BEGIN IMMEDIATE`, foreign keys, busy timeout, and append-only history protections. IDs include text work-item keys and AUTOINCREMENT integer histories/claims/seams/events/message records. JSON is stored as TEXT in several columns. Migration must preserve keys, foreign-key links, ordering/append-only semantics, exact text/JSON meaning, and next identity values. Adapt serialized UTC timestamps and SQLite error/locking behavior deliberately. Avoid changing claim fencing or request outcomes while porting SQL.

## Data beyond SQLite

- The REST app reads Git for work-item briefs and queue/build facts. `POST /items` checks a brief from configured repository refs; `/mastertodo.md` and `/items/{id}/brief.md` are repo-backed. Seam and review state also depends on refs/commits and scanner output. Git remains a separate authoritative input; a DB snapshot cannot recreate refs, branches, or brief files.
- `SKYKEEP_TODO_READINESS_FILE` names readiness JSON. `SKYKEEP_TODO_REVIEWS_DIR` holds review records. Their config and current content are outside SQLite. Include a read-only inventory/hash and cutover consistency policy; do not assume SQLite snapshot freezes them.
- Hashed box tokens, service config, repo checkout, systemd unit/timers, and generated keeper config are machine-local. Keep secrets out of migration artifacts. Inventory existing source/copy versions and deploy paths separately.
- The documented deployment flow uses `scripts/todo_service/cutover.sh`, then distributes keeper client/config through the owner runner to jeltz, wonko, wowbagger, and aragog. The sync timer and scanner can write DB state, so they and direct admin/import/worker writers must be stopped or fenced along with the API during final snapshot/import.

## Cutover and rollback shape

R3 proposes a one-time offline mailbox transfer and says to migrate the rest later. That scope is superseded by the owner’s latest direction: migrate all REST persistence early. Keep the useful offline mechanics, but widen the snapshot/import/validation to all 27 tables and sidecar authority. Rehearse against a disposable dedicated tooling PostgreSQL database; do not reuse the customer vault DB or a gate DB. Use SQLite’s online backup API to obtain a consistent WAL snapshot after quiescing writers, not a file copy. Reconcile/expire active leases safely before freeze. Import transactionally, preserve IDs, and verify per-table counts, canonical row hashes, foreign keys, table-specific invariants, and PostgreSQL sequence heads. Compare the external repo refs/brief and JSON sidecars before/after; record which sidecars remain external authorities.

Keep current listener, URLs, auth, and response shapes while switching store authority. Before acceptance, rollback can restore old code plus original frozen SQLite authority. After PostgreSQL accepts writes, keep PostgreSQL authoritative; rollback must use compatible code or replay changes, never silently reopen the old SQLite DB. Unknown: actual live SHA, schema/data counts, active claims, SQLite path/WAL size, backup recovery proof, PostgreSQL host/role/database readiness, and exact running timer/process inventory. These need safe operator-side discovery before a downtime window; secrets remain unread.

## Token-cheap coherent packets

1. **Contract inventory and PostgreSQL schema:** map all 27 tables and every route/direct writer to schema and invariants; choose dedicated DB/role and record sidecar authorities.
2. **Store/API port:** port the complete store plus SQL-using service modules to PostgreSQL; keep the existing REST paths and `TodoClient` contract. No dual-backend abstraction or parallel API daemon.
3. **Snapshot importer and cutover:** manifest all table rows/IDs, rehearsal and validation; stop every writer, import once, deploy/restart service and callers, then exercise key read/write flows and restart persistence. Maintain compatible rollback boundary.
4. **Skymaker hello-world:** separate, tiny visible endpoint/site slice. It should not become a substitute for the full DB cutover. Unknown: whether “Skymaker” is a new listener/site or a route hosted by the existing service; settle this boundary in planning.

## Session-log mining item and scanner worktree

`/home/kevin/my_code/skykeep-todo-live/MasterToDo.md:148` has priority-3 `TOOLING-TOP-20-SKILL-SCRIPT-COMBOS-FROM-SESSION-LOGS`; its brief measures the last seven days of 266 Claude JSONL files/27,355 calls, with estimated shell-command token cost. `/tmp/skykeep-log-scanner/todo/LOG-REPEAT-SCANNER.md` is a narrower child brief for fixed-category, counts-only mining of Claude/Codex/Grok logs. The temp checkout is clean on `seam/LOG-REPEAT-SCANNER`; metadata shows `scripts/agents/session_mine.py`, `docs/dev/session-mine.md`, and `tests/unit/test_session_mine.py` already exist. No log content was opened and no model analysis was run. Their existence plus the still-present brief means completion/landing status is unresolved; check its Git history/queue separately.

## Source anchors and unknowns

- Main checkout CodeGraph: `scripts/todo_service/store.py` v2 DDL/transactions and `app.py` route/client semantics. It is not the target baseline.
- Target checkout (no CodeGraph index): `scripts/todo_service/store.py` v19 DDL at lines 80–568 and later migrations; `app.py` route decorators from lines 590–1797, `/dump` at 843–867, external brief lookup around 184–209, config around 281–317, message routes 979–1015.
- Deployment: `docs/dev/todo-service-install.md` §§ 1–5; design contract: `docs/planning/todo-service.md` §§ 4–5, 8–11, 16; Cord scope conflict: `docs/design/cord_plan_20261008.120240.r3.md`; target checkout and mailbox findings: `docs/design/cord_plan_20261008.120240.architecture-review.md` lines 16–42.
- Seven-day mining: `MasterToDo.md:148`, `todo/TOOLING-TOP-20-SKILL-SCRIPT-COMBOS-FROM-SESSION-LOGS.md`, `/tmp/skykeep-log-scanner/todo/LOG-REPEAT-SCANNER.md`.
