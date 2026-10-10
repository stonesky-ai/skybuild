# Petri acceptance and promotion handoff

Task: `SKYBUILD-PETRI-12`. Priority: 2.

This handoff records implementation evidence. It does not authorize deployment, live migration, task completion or paid inference.

## Source and contract versions

The final branch must contain `docs/design/petri_workflow.md` and its related architecture, task workflow, implementation plan and ADR changes.
The approved design head is `c143b9be212cd4856f371406768ab117bccb4f16`.
The prepared checks use task 05 head `8c792ebe9e4df5536063c7abbcf61c3e37968821`.
The Petri storage schema is version 1 in migration `013_petri_workflow.sql`.
Record the final source tree, all parent heads, project requirement profile and acceptance policy versions before integration.

## Required acceptance evidence

| Area | Required evidence |
| --- | --- |
| Pure transitions | Every declared source place and rejection, all five validation stages, failures, pending controls and reopening. |
| Projects | The same task ID in two projects remains isolated. Reject unauthorized reads, writes and result identities. |
| Transactions | Two claimers, pass/fail ordering, lost response replay, changed payload, journal rollback and stale fence rejection. |
| Invalidation | Head, base, definition, dependency and policy changes preserve history and invalidate affected evidence. |
| Recovery | Due date and milestone resume, Hold release, split/merge lineage and unresolved effect ownership. |
| Integration | Freeze fixed inputs, confirmed failure, exclusion before dispatch, uncertain publication and exact accepted inclusion. |
| No publication | Explicit current policy, satisfactory requirements and acceptance evidence without an invented merge. |
| Migration | Disposable snapshot rehearsal, journal preservation, ambiguous legacy phases in Hold and no inferred completion. |
| Workbench | Seven places, complete filtered totals, current revisions, safe display, disabled reasons and keyboard controls. |

## Bounded concurrency model

`PetriWorkflow.tla` checks two workers, two claim fences, one task and one external effect.
SANY passes. TLC passes with 571 generated states and 285 distinct states, at depth 10.
The broken fence and late-pass configurations each produce the intended invariant violation at depth 5.
The JVM heap is limited to 256 MiB. TLC uses one worker.
See `../models/PetriWorkflow.md` for assumptions and limits.
Reuse the existing Reassessment and Publication models for their unchanged requirements.

## Current evidence and remaining controls

The prepared pure acceptance checks pass: 11 tests at the task 05 source snapshot.
The exact combined source, independent review, PostgreSQL acceptance and full gate are pending.
No live task snapshot has been migrated. No accepted service has been replaced.
A passing bounded model is not proof of SQL or remote publication correctness.

The final record must identify the frozen branch heads, independent exact-head review, combined gate receipt and published task inclusion.
Keep runtime promotion separate from repository integration.
