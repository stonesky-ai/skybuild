# ADR 0027: Daily committed PostgreSQL dumps and journal-based recovery

Date: 2026-10-08. Status: accepted owner direction for scheduled daily dump-file commits and a low-priority restore/rebuild task; publication and journal mechanics remain open. Supersedes [ADR 0025](0025-deferred-backup-and-restore.md). Implementation: not started.

## Decision

The owner reversed the backup deferral. Schedule a CPU task that dumps the dedicated SkyBuild PostgreSQL database to a file committed daily. Follow the normal GitHub push workflow and distinguish a local commit from confirmed remote preservation. SKYBUILD-DAILY-DB-BACKUP owns this item.

Move SKYBUILD-BACKUP-RESTORE from deferred to proposed at low priority, retaining its stable ID. Create and test database restore and reconstruction based on artifacts in a journal file committed periodically. Journal location and commit interval are explicitly undecided. Do not make the full recovery drill or a finished journal design a prerequisite for the first launch-free service or daily dump job.

## Proposed mechanics and limits

Use a bounded scheduled `pg_dump` job with complete-file publication, an identity/version/hash manifest and an isolated backup worktree. Prevent overlap; handle laptop-offline catch-up and failed dump/commit/push without replacing prior good artifacts or claiming an unconfirmed remote copy. Choose format, Git destination/path, run time, retention/size and encryption/key custody before enabling publication. Keep keys/credentials outside Git and preserve database separation.

The journal needs recoverable artifact content/references and hashes, stable event/effect IDs, schema/source/config versions and a validated boundary relative to the dump. Reuse existing history where suitable. Implement the minimal journal writer/periodic commit once location/interval are selected. Test a fresh disposable service rebuild and database restore from pinned source/artifacts, plus repeatable journal reconstruction and missing/corrupt coverage. Replay cannot automatically repeat production effects, clear stops, renew approvals, reset usage or launch workers; deliberate CPU rebuild tests retain their own bounded authority. State recovery coverage honestly; nothing guarantees recovery of artifacts that were never preserved.

## Public constraints and implementation implications

PostgreSQL documents `pg_dump` as a consistent single-database export; cluster-wide roles/tablespaces are outside that export. Account for needed provisioning/configuration separately without dumping a shared cluster or the application vault. [Official pg_dump documentation](https://www.postgresql.org/docs/current/app-pgdump.html).

A logical dump and an application journal do not by themselves constitute PostgreSQL WAL-based point-in-time recovery. No WAL/PITR subsystem is selected here. [Official continuous-archiving documentation](https://www.postgresql.org/docs/current/continuous-archiving.html).

Architecture: sections 3–4, 7–9 and 16. Implementation: the two proposed tasks in the current ledger and the parent plan’s database-backup/recovery section. No dump, commit/push of database data, schedule installation or restore was performed during planning.
