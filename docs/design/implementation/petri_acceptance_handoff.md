# Petri acceptance and promotion handoff

Task: `SKYBUILD-PETRI-12`. Priority: 2.

This handoff records implementation evidence. It does not authorize deployment, live migration, task completion or paid inference.

## Source and contract versions

The final branch must contain `docs/design/petri_workflow.md` and its related architecture, task workflow, implementation plan and ADR changes.
The approved design head is `c143b9be212cd4856f371406768ab117bccb4f16`.
The Task 12 branch contains the following pinned implementation parents:

| Parent | Head |
| --- | --- |
| Shared API contract, Task 05 | `8c792ebe9e4df5536063c7abbcf61c3e37968821` |
| Worker adapters, Task 06 | `d193fa6ee50da1123ef5fd0bb7aa2598f510e95c` |
| Validation adapter, Task 07 | `71b1345d3502e85dbea04b6cbe23070e30c34c7c` |
| Integration adapter, Task 08 | `c1d3eb85f08a787adb92f7c74b28d0250c84a03b` |
| Recovery, Task 09 | `bbce72377e7070b7ef5fcda69bfa3de304e93798` |
| Claim capability, Task 04 correction | `cdb66d2e60de4a44c45205ec7570c5e799e738a0` |
| Workbench controls, Task 11 | `2b9d731a4e2b6545eaf5a9a16fd3b5027667e64a` |

The default requirement profile is `petri-checks-v1`. It requires all five validation stages.
A stage omission needs a current not-applicable result with an explicit policy reason.
New tasks use the profile automatically. A sufficient definition starts in Ready, with legacy status and phase both set to ready.
Incomplete definitions start in Hold. A definition edit retains Hold until explicit release.
Creation and claim do not grant spending or external launch permission.
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
SANY passes. TLC passes with 1011 generated states and 425 distinct states, at depth 12.
The broken fence and late-pass configurations each produce the intended invariant violation at depth 5.
The JVM heap is limited to 256 MiB. TLC uses one worker.
See `../models/PetriWorkflow.md` for assumptions and limits.
Reuse the existing Reassessment and Publication models for their unchanged requirements.

## Current evidence and remaining controls

The final targeted source checks pass: 234 tests, with 18 PostgreSQL cases skipped because no disposable DSN was selected.
These checks include enrollment, cross-project identity, integration observations, exclusions, the transition catalogue and claim capability.
The disposable PostgreSQL acceptance includes a real API creation, atomic claim, submission, five validation stages and explicit no-publication acceptance.
It also checks default creation through the real manual dispatcher and assignment verifier, incomplete definition release, legacy snapshot enrollment, old accepted completion and journal receipts.
The existing compatibility fixtures construct pre-Petri tasks by replacing only a pure enrollment helper.
They do not edit journal snapshots, disable triggers or add a production bypass.
Legacy completion without matching Petri inputs and stage evidence enters diagnostic Hold; its completion record remains unchanged.
Independent exact-head review, PostgreSQL acceptance and the full combined gate are pending.
The exact branch head and tree belong in the independent review and combined gate receipts.
Targeted test results and bounded model results do not substitute for those receipts.
No live task snapshot has been migrated. No accepted service has been replaced.
A passing bounded model is not proof of SQL or remote publication correctness.

The final record must identify the frozen branch heads, independent exact-head review, combined gate receipt and published task inclusion.
Keep runtime promotion separate from repository integration.
