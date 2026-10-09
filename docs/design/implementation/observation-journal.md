# Launch-free observation journal scaffold

This task derives from architecture A34 sections 8–10 and implementation plan section 5 at base commit 602778093613adf7eeb0cc38328236f2f169d095. Governing source SHA-256 hashes:

- `docs/design/architecture.md`: `f4f2c2cbe93132a36177aa6baf6728c4a207af8f0254770c6b3e80f605533f85`
- `docs/design/implementation_plan.md`: `0c192be35309903a4c72a9a887057c58c9dcb538bc0d81af87db7249a7a23d25`

The Store accepts bounded evidence packets for an existing CPU reservation, task and claim fence. Current project authorization is checked on every receipt/read. Only the attempt actor or owner/admin may report. Evidence contains stable event, task, attempt, source, component, boot, PID/start identity, source sequence, observed/received times and up to eight opaque artifact references. Artifact contents, uploads, retention and credential redaction remain outside this scaffold; callers must not put secrets in references.

Migration 009 creates immutable `observation_events` and a cached projection. Event ID and identity/sequence are unique per project. Identical authenticated replay returns the original receipt; conflicting replay is rejected. Receipt and projection commit together. The project advisory lock serializes receipt conflict checks. Shared reservation and claim locks prevent cancellation or ownership changes racing eligibility.

The first current, reserved attempt identity pins each task/component/source projection. Only that exact identity with a strictly higher source sequence advances it. Reordered, changed boot/PID/start and released/stale ownership evidence remains historical. Identity rollover is deliberately unsupported until explicit reconciliation exists. Source IDs are caller-declared evidence, not qualified independent observer enrollment. A cached projection is last received evidence, not a claim of present health. Clock timestamps never override source ordering; lease expiry never proves death. There is no inferred freshness, automatic death detection or accepted completion.

Observations never release ownership or reservations, settle effects, renew authorization, grant launch authority or accept completion. Reported exit/result and missing heartbeat remain evidence only. Markdown task authority remains unchanged. No live database, worker, daemon, launcher, probe, service or deployment is part of this task. No REST surface is added.

`FakeObserver` replays deep-copied caller packets through the Store. PostgreSQL is the durable receipt journal; this in-memory fixture is not a durable offline host spool. Durable local spooling, disk quotas/retention, offline deadlines, independent physical observation and host qualification remain later work. This scaffold establishes no offline host guarantees.

## Verification

Final checks: full suite `216 passed, 19 skipped`; the separately enabled importer/HTTP integration files `20 passed` (including all 19 environment-skipped cases). Observation tests `6 passed`. All schema-version expectations now require migration 009. `git diff --check` passes. The existing Starlette/httpx deprecation warning remains.

PostgreSQL tests use only the task-owned `skybuild-pr015-observation-test` container and disposable `skybuild_test_observation` database. Tests cover restart replay, conflicting replay, source reordering, simultaneous receipts, boot/PID reuse, scoped authorization, bounded input, append-only SQL guards, expired/released ownership, retained reservations/effects and a crash before commit rolling back both journal and projection.

[`ObservationJournal.tla`](../models/ObservationJournal.tla) and [`ObservationJournal.cfg`](../models/ObservationJournal.cfg) models two identities, three source sequences, duplicate/reordered receipt and crash/reconnect. SANY passes. TLC explores 226 distinct states with no invariant violation. Removing the matching-identity condition produces a three-state counterexample: receive identity 1 sequence 1, then identity 2 sequence 2; the projection points to nonexistent identity 1 sequence 2. This checks bounded receipt design, not physical processes, database durability settings, hostile SQL writers, event payload conflict validation or offline host behavior. Atomic Store transactions and serialization are implementation assumptions.

Implementation phase: ready for independent review after final checks. Responsible: observation journal task author; independent reviewer/integrator owns the next action. Integration and runtime adoption remain pending.
