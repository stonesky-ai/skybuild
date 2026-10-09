# ADR 0001: SkyBuild product and database boundary

Date: 2026-10-08. Status: accepted from explicit owner direction. Implementation: not started.

## Context

SkyKeep mixes vault application code with build APIs, Keeper, launchers, integration, observation and deployment tooling. Different source/deployed copies and independent launch paths obscure authority and behavior. The owner selected the dedicated stonesky-ai/skybuild repository and requires a database separate from the vault.

## Decision

Extract all build tooling into SkyBuild, including its generic skills/docs/tests and operational entry points. Cord and Keeper retain their names within the build product. Product repositories keep application code/tests and a pinned versioned build adapter with project-specific commands and policies.

Use a dedicated SkyBuild PostgreSQL database, credentials, migrations, backups and lifecycle, separate from SkyKeep's application database and disposable tests. A second schema in the vault database does not satisfy the boundary. Physical hosting remains open. Complete extraction is required; concurrent autonomous multi-project use can follow later.

## Alternatives and consequences

Leaving the platform inside SkyKeep or repurposing SkyTrends/Dark Build Factory conflicts with the selected product boundary. Sharing the vault database couples permissions, migration and recovery and was explicitly rejected.

The census must include every writer/launcher/installed artifact, not only obvious Python modules. Temporary thin wrappers need explicit retirement. Mechanical moves, SQL conversion and launcher adoption remain separately reviewable. SkyKeep is a client project after extraction.

Architecture: sections 2 and 7. Implementation implications: census, full API/writer migration, pinned project adapters and complete extraction audit.

Later owner direction: [ADR 0025](0025-deferred-backup-and-restore.md) expressly defers backup/restore work until further notice and removes backup-related bootstrap/cutover/migration prerequisites. Database separation and the accepted outage policy remain in force.

Subsequent owner change: [ADR 0027](0027-daily-dumps-and-journal-recovery.md) supersedes ADR 0025 with scheduled daily dump-file commits and a proposed low-priority restore/journal-rebuild task. Journal location and commit interval remain undecided.
