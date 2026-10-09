# ADR 0002: Task bootstrap and authority transition

Date: 2026-10-08. Status: accepted owner direction for the file set and REST/PostgreSQL destination; import and cutover mechanics are proposed for review. Implementation: not started.

## Context

SkyBuild needs its own planning/tasks before it has a service that can store and serve them. The owner requested mastertodo.md, deferred.md and alreadydone.md, then an initial PostgreSQL-backed REST todo server and the minimum communications needed to begin coding.

## Decision

Keep the three ledgers in docs/design with stable task IDs and one record per ID across the set. They are the SkyBuild task authority until the bootstrap's accepted cutover. Provide project-scoped todos, full briefs/history and a durable minimal Cord mailbox before distributed execution.

Proposed mechanics: freeze a commit/hash, import all three ledgers losslessly into a disposable rehearsal then the accepted destination, validate, record the authority switch and turn Markdown ledgers into read-only generated exports. PostgreSQL through the API becomes the sole editable task authority. Git retains architecture/ADRs and source, not a second live task database. Bootstrap task/API usefulness does not require fleet launch or the entire legacy cutover first; coexistence must be disjoint by project/domain.

## Alternatives and consequences

Using the old SkyKeep queue for new SkyBuild planning couples the new project to its extraction source. Indefinite two-way Markdown/API sync creates conflicting writers and is rejected. Waiting for every legacy domain before a todo/mailbox service is useful unnecessarily expands the bootstrap.

The importer must preserve deferred/completed tasks and evidence, not only current rows. Before first API mutation a file rollback is possible; afterwards recovery must reconcile API history. Concurrent executors require later fenced claims. A mailbox message cannot authorize execution.

Architecture: sections 3–7. Implementation implications: bootstrap first, explicit task cutover second, full legacy migration before broad adoption.
