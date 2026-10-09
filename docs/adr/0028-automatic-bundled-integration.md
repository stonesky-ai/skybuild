# ADR 0028: Automatic integration through reviewed task bundles

Date: 2026-10-08. Status: accepted owner direction for automatic merge after required tests/review, with core bundled integration; mechanisms and numeric limits proposed. Implementation: not started.

## Context and decision

The owner confirmed automatic merging into the configured integration branch after required tests/review pass, then specified bundling because task production can exceed integration throughput. An hour-long integration run with twenty task implementations arriving meanwhile is illustrative, not a fixed batch or benchmark.

Use task bundles as the normal integration unit. Collect ready reviewed tasks, freeze member heads/dependencies and target/base into a combined candidate, run the required combined gates, and automatically publish the exact passed result within existing authority. New tasks collect for the next bundle while gates run. No fresh owner merge prompt or full integration suite per task is required by default. Core bundling is required before broad automatic integration; optional cloud-batch optimization remains deferred. Runtime deployment/promotion retains its existing policy.

## Proposed compact mechanics and failure boundaries

Reuse integration-set/scan state and one fenced Runner/Keeper integrator per project/target. Exact heads/base/policy/config and candidate bind evidence; choose the strongest required profile across members. Integration-only edits require fresh review and a new candidate. A changed head/base needs a new candidate/evidence. Prefer one bundle publication flow, potentially a bundle PR, with confirmed remote commit and per-task PR/inclusion reconciliation before acceptance or cleanup. Do not bypass branch protection or treat a closed PR as merged.

A30 clarification: [ADR 0030](0030-effect-boundaries-and-accounting.md) replaces the ambiguous strongest-profile shorthand with the union of required checks and adds external publication enforcement. [ADR 0031](0031-task-workflow-and-append-only-journal.md) requires actionable per-task state and append-only history for every bundle outcome. These refine mechanics while retaining this ADR's accepted automatic-bundling direction.

Failed bundles remain unintegrated. Diagnose/split under bounded authority, preserve dependencies and gate every changed subset; do not publish untested subsets or loop unchanged long suites. Reconcile uncertain merge outcomes before reassignment. CPU status reports backlog/age, active candidate, gate duration and accepted throughput; cap queued resources and apply admission backpressure when integration cannot keep up. Coalescing/size/age defaults, failure-diagnosis limits, review qualification and exact publication mechanism remain open.

## Reuse and validation

The [source follow-up](../design/research/bundled_integration_20261008.md) confirms useful integration-set structures, but the legacy light runner skips major gates and leaves PR closure to the owner. Adapt rather than copy that behavior unchanged. Validate new arrivals, stale evidence, mixed profiles, dependencies, failed/subset bundles, concurrent integrators, crash/restart and unknown remote acknowledgments.

Architecture: sections 2, 8–10, 13 and 15. Implementation: area 5a and SKYBUILD-BUNDLED-INTEGRATION. No merge, gate run or provider call was performed during planning.
