# ADR 0007: Configurable outage behavior and deferred laptop failover

Date: 2026-10-08. Status: accepted owner direction for default/UI choice and deferral; detailed offline authorization and failover protocols remain proposed. Implementation: not started.

## Context

The initial controller and dedicated PostgreSQL database run on the owner's laptop. The laptop can sleep, shut down or lose connectivity while a remote worker is already executing a task.

## Decision

Expose loss-of-controller behavior in the control website. Default to finishing the current task. Offer checkpoint/stop at the next safe point as an alternative. This replaces the earlier proposed checkpoint/stop default for contact loss; it does not change an explicit operator stop or emergency termination policy.

Carry the selected policy/version with the current task's authorization so the worker can act while disconnected. Continue only the already-authorized task; do not treat the outage as permission to claim another task, widen scope or create an unapproved retry. Buffer bounded result/evidence updates for idempotent reconciliation when connectivity returns. Detailed offline budget/deadline behavior still needs review.

Plan a second configurable laptop as a possible HA/DR hot-failover host only at very low priority after core capabilities exist. It is not a bootstrap dependency or permission to add another writable authority now. Basic backup/restore remains a separate requirement.

## Consequences

Do not reassign a disconnected worker's task merely because its lease expired. Keep uncertain ownership/exposure visible until completion or fenced reconciliation. A website policy change or stop is pending for a disconnected worker until acknowledged; the UI cannot promise instant delivery.

Later HA requires replicated/recoverable data, one fenced active controller, preserved in-flight authorization and tested recovery; a lost heartbeat alone cannot safely promote another writer. Detailed HA protocol and recovery targets can wait.

Architecture: sections 3, 8–9, 12 and 15–16. Implementation implications: website policy, cached authorization, offline completion/reconnect checks, and a very-low-priority deferred HA/DR item.

Later owner direction: [ADR 0025](0025-deferred-backup-and-restore.md) expressly defers backup/restore work until further notice and removes backup-related bootstrap/cutover/migration prerequisites. Database separation and the accepted outage policy remain in force.

Subsequent owner change: [ADR 0027](0027-daily-dumps-and-journal-recovery.md) supersedes ADR 0025 with scheduled daily dump-file commits and a proposed low-priority restore/journal-rebuild task. Journal location and commit interval remain undecided.
