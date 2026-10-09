# ADR 0004: Controlled launch and stop boundary

Date: 2026-10-08. Status: proposed; numeric policies and local physical launch protocol remain unresolved. Implementation: not started.

## Context

Historical Keeper/ensure, role respawner, fleet-watch and watchdog paths can independently launch sessions. A stopped process need not be durably disabled. PostgreSQL admission and OS process creation cannot be one atomic transaction, and missing contact does not prove death.

## Proposed decision

Require one deterministic admission contract for all managed launchers. Atomically reserve action ownership, concurrency, budget and resources under current restrictive controls/generations. Use single-use fenced permits and durable local launch journaling/reconciliation. Keep reservations while execution is uncertain. Compose central and local stops; a stale enable or message cannot clear them.

A stop immediately fences new admissions but is only acknowledged effective for a box after reconciling in-flight starts at that generation. Partitioned/unacknowledged boxes remain visibly pending/unknown and follow bounded local policy. Independently supervised observers provide death/progress evidence, without another uncontrolled paid-restart path.

## Alternatives and consequences

Prompt-only checks, independent launcher permissions and expiry-based assumption of death permit duplication or spend outside control. Claiming a universal instantaneous stop at database commit would misrepresent the physical boundary.

Bootstrap todos/Cord remain launch-free. Before a canary, select spend/deadline policies and verify the local journal, crash/replay, stop acknowledgment and observation protocol. [ADR 0007](0007-outage-policy-and-deferred-failover.md) settles the contact-loss default/UI choice; explicit stops and detailed offline limits remain separate. The small admission model checks transaction-level properties only; extend it before relying on physical launch guarantees.

Architecture: sections 8–10. Implementation implications: controls precede adoption; adapt or disable every bypass path; do not release uncertain exposure on timeout alone.
