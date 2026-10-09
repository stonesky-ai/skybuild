# ADR 0006: Host the initial control service on the owner's laptop

Date: 2026-10-08. Status: accepted from explicit owner direction. Implementation: not started.

## Context

The owner has no always-on machine and wants the initial SkyBuild service to use the current laptop. A separate server is an option if the laptop proves insufficient, not an initial prerequisite.

## Decision

Run the initial SkyBuild API and its dedicated PostgreSQL database on the owner's laptop. Keep the database distinct from SkyKeep's application and test databases. Preserve the laptop's interactive resource needs; do not assign heavy build workers or model serving merely because it hosts the controller.

Discuss measured resource or other suitability constraints with the owner before moving the service or provisioning another host. Do not automatically rent a server or upgrade/reimage the laptop. The earlier proposed dedicated always-on server is a future option only.

## Consequences

Persistence and continuous availability are different: task/message data must survive a normal service/laptop restart, but REST is unavailable during sleep, shutdown or loss of connectivity. Clients must report unavailable rather than silently write a fallback store. Remote-worker outage behavior is now specified by [ADR 0007](0007-outage-policy-and-deferred-failover.md): finish-current-task by default, checkpoint/stop selectable in the control website; detailed bounds need review before adoption. Backups, restore, network reachability and resource limits remain planning questions.

Later clarification: [ADR 0023](0023-initial-tailscale-access.md) settles the initial network choice as Tailscale with reachability from the intended enrolled boxes. Concrete setup and backup/restore remain to be qualified.

Architecture: sections 3, 12 and 15. Implementation implications: laptop service/backup/restart planning, sleep/outage acceptance and resource measurement; no initial cloud provisioning.

Later owner direction: [ADR 0025](0025-deferred-backup-and-restore.md) expressly defers backup/restore work until further notice and removes backup-related bootstrap/cutover/migration prerequisites. Database separation and the accepted outage policy remain in force.

Subsequent owner change: [ADR 0027](0027-daily-dumps-and-journal-recovery.md) supersedes ADR 0025 with scheduled daily dump-file commits and a proposed low-priority restore/journal-rebuild task. Journal location and commit interval remain undecided.
