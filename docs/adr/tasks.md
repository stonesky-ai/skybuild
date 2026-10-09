# Task authority and workflow decisions

Each section retains its original ADR ID. Status applies to the decision, not implementation. [Architecture](../design/architecture.md) governs current requirements.

<a id="adr-0002"></a>
## ADR 0002: Task bootstrap and authority transition

**Accepted ledger and destination; cutover mechanics proposed, 2026-10-08.** Keep `mastertodo.md`, `deferred.md` and `alreadydone.md` as sole SkyBuild task authority until an explicit validated cutover. Preserve stable IDs and one record per ID. Provide project-scoped tasks, full briefs/history and minimal durable Cord before distributed execution; messages grant no execution authority.

Proposed cutover: freeze source commit/hash, import all three ledgers losslessly through a disposable rehearsal into PostgreSQL, validate, record the switch and make Markdown read-only exports. API/PostgreSQL then becomes sole editable task authority. Do not use SkyKeep's queue or dual-write Markdown/API. Preserve completed/deferred evidence; after first API mutation, recover by reconciling API history, not restoring files blindly. Legacy domains migrate separately under explicit authority. Architecture sections 3–7.

<a id="adr-0029"></a>
## ADR 0029: Task is the canonical work term

**Accepted, 2026-10-08.** Use `task` in new design, UI, client and API; the proposed route is `/api/v1/projects/{project_id}/tasks` with `task_id`. Preserve ledger filenames, stable IDs and exact legacy/source identifiers when required. Map legacy terms to one task identity, not parallel queues; inspect relationships before combining rows.

Attempts, processes and workers describe execution. Bundles group tasks. Keep CPU attempts and model queries distinct within a task. Do not mechanically rename historical databases, routes, branch prefixes or IDs. Architecture sections 1 and 4–7.

<a id="adr-0031"></a>
## ADR 0031: Explicit workflow and append-only journal

**Accepted workflow, early task page and immutable history; mechanics proposed, 2026-10-08.** Every unfinished task shows phase, next action or awaited event, responsible party, blocker and evidence. Track review, rework, dependencies, testing, rebasing and every bundle outcome under the same task ID. A dirty input schedules reassessment, not an unexplained failure.

Provide an early task page for scope/definition edits, rework, split/merge, dependency changes and date/milestone deferral. Append each action and correction to the journal; never edit or delete historical events. Keep definitions and projections mutable, while preserving prior versions, attribution, evidence and lineage. Defer multi-person approval chains; preserve actor identity now. A planning scan may propose changes but cannot silently alter accepted scope or spending authority.

Proposed mechanics: commit mutations, journal events and invalidations together; evaluate captured input generations and reject stale projection writes. Use expected revisions, cycle checks and budget/ID lineage for structural edits. Reconcile old effects before replacement work. Deferral triggers reassessment, not automatic model launch. Keep stalled transitions visible with a named resolver. See [workflow contract](../design/task_workflow.md) and architecture sections 5, 9 and 13.
