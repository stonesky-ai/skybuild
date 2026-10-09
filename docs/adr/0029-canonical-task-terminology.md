# ADR 0029: Task is the canonical work term

Date: 2026-10-08. Status: accepted explicit owner direction. Implementation: planning terminology only.

## Decision

The owner specified that `seam`, `task`, `todo` and `job` mean the same work unit and prefers task. Use task in current design, UI, API/client vocabulary and new code. The proposed REST contract uses `/api/v1/projects/{project_id}/tasks` and `task_id`. No implementation of the former `/todos` proposal exists to migrate.

Preserve the existing mastertodo.md/deferred.md/alreadydone.md ledger filenames, stable task IDs, historical reports and exact external/source/schema identifiers where compatibility or provenance requires them. Legacy names are mapped aliases/history, not separate queues or duplicated task records. Inspect real legacy relationships before combining physical rows; preserve IDs, attribution and full evidence.

Execution attempts/processes and workers are technical execution entities. An integration bundle groups tasks. These are not alternate user-facing work units. Preserve distinct model-query and CPU-attempt lifecycles within a task, including the accepted expiry cutoff and independent reporting behavior.

## Consequences

Normalize active architecture, implementation planning, ledger prose and instructions now. Historical ADR/research wording and legacy identifiers may retain prior terms. New field/route names and migration mapping follow task vocabulary; avoid mechanical renaming of source databases, existing routes, branch prefixes or stable IDs. An implementation-ready task remains awaiting acceptance while required bundle integration is pending.

Architecture: sections 1, 4–7. Implementation: bootstrap task contract/client and explicit legacy mapping. No source/API/database was renamed or migrated during planning.
