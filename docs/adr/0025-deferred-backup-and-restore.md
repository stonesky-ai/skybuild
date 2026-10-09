# ADR 0025: Defer backup and restore work until further notice

Date: 2026-10-08. Status: superseded by [ADR 0027](0027-daily-dumps-and-journal-recovery.md). Historical owner deferral retained below. Implementation: not started.

## Context and decision

The owner accepted encrypted daily backups off the laptop and backups before migrations as a later goal, but explicitly deferred the work until further notice. Projects presume source/docs and committed handoffs are pushed to GitHub.

Record SKYBUILD-BACKUP-RESTORE only in the deferred ledger. Do not implement backup automation, storage/key management or restore drills now, and do not make that work a prerequisite for bootstrap, API task cutover, migration or core extraction. Reopen only on further owner direction. This supersedes earlier planning language requiring backups before cutover or as earlier hosting work.

## Consequences

GitHub preserves pushed tracked content; it does not preserve unexported PostgreSQL task/history/Cord state or uncommitted work. Record this limit without introducing unrequested database exports or a second writable authority. Normal persistence/restart, consistent migration reads, database separation and import validation still belong to their existing contracts; they do not authorize backup implementation under another name.

Destination, retention, key custody and restore/authority reconciliation remain later choices. HA/DR is separately deferred and does not silently reopen backups.

Architecture: sections 3–4, 7 and 16. Implementation implications: remove backup/restore checks from initial acceptance and hosting prerequisites; preserve the later goal in the deferred ledger. No backup was created, configured or tested.
