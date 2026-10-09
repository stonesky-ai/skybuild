# ADR 0031: Explicit task workflow and append-only journal

Date: 2026-10-08. Status: accepted owner requirements for workflow, task page, immutable journal and much-later team approvals; mechanics proposed. Governing architecture: sections 5, 9 and 13. Derived detail: [task workflow](../design/task_workflow.md).

## Context

The owner reports a major SkyKeep failure: tasks were code-complete but unmergeable, and nobody knew which condition was wrong or who should repair it. Existing history, scan and integration-set records are useful but do not by themselves guarantee a known next step. The owner wants a Jira-style flowchart and an action-to-state contract, with a journal spanning the entire task lifecycle.

## Decision

Every unfinished task exposes a phase, next action or awaited event, named responsible component/person, specific blocker and relevant evidence. Dirty input or a reset schedules the right evaluator; it is not an unexplained failed status. Preserve testing, retesting, review, rework, rebasing, dependency changes, integration attempts and every bundle inclusion/exclusion/outcome under one task identity.

Provide an early outstanding-task page with status and actionable detail. The owner can edit description, scope, definition of done and considerations, request rework/reassessment, split or merge tasks, change dependencies and defer until a date or major milestone. Journal those actions and their consequences. A bounded model planning scan may propose considerations, decomposition and estimates under existing model controls; it does not silently change accepted scope or spending authority.

The task journal is append-only. Corrections append events; no journal edit/delete API or runtime database mutation privilege is provided. ADRs cannot authorize rewriting historical journal records. Current task definitions/projections can change, but previous definitions, attribution, evidence and lineage remain recorded. Split/merge preserves replaced IDs and links rather than deleting history or claiming successful completion.

Multi-person approval chains for teams cooperating on a build are explicitly deferred until much later. Keep actor identity and event history now; do not add an approval-chain engine to the initial workflow. This deferral does not settle the still-open qualification/independence policy for the current required technical review.

## Proposed mechanics and failure handling

Use the existing Store, task/history, attempts, artifacts and integration records. One explicit transition function and a small PostgreSQL-backed reconciliation loop are sufficient. Commit mutations, journal events and invalidation markers together. Evaluate a captured input generation, reject stale projection writes and never clear a newer invalidation while acknowledging older work. Late results retain their original inputs and cannot make changed work ready.

Scope/structural changes use expected revisions, atomic dependency-cycle checks, explicit acceptance/dependency mapping and preserved budget lineage. Reconcile existing attempts/publications before replacement work. Deferral triggers reassessment rather than automatic model launch or approval renewal. Unchanged failures back off with a named resolver; CPU observation detects stalled/ownerless transitions.

## Reuse and delivery

The [mergeprep inspection](../design/research/task_workflow_reuse_20261008.md) identifies useful fact/verdict/staleness behavior to adapt. It is not evidence that the old system already meets this lifecycle contract.

Bootstrap records manual phase/next-action metadata and immutable history. SKYBUILD-TASK-WORKBENCH provides the early page after task authority cutover. Execution controls add automatic reconciliation; integration adds per-member bundle outcomes. Model scanning becomes available only after model admission qualifies. Periodic Git export/recovery of the journal remains separate low-priority recovery work; the online task journal is required core functionality. SKYBUILD-TEAM-APPROVAL-CHAIN records the explicit later feature.

Required validation covers concurrent scope/dependency edits, split/merge during execution, stale tests/reviews, rebase, invalidation during evaluation, bundle failures/exclusions, lost merge acknowledgment, reset, date/milestone wakeup and attempted journal modification. Every case has a defined state/action and retained history. No runtime behavior has been implemented or exercised by this ADR.

The [reassessment design model](../design/models/Reassessment.md) passed SANY and 10,000 sampled TLC traces of depth 20 (200,001 states checked). A deliberate broken acknowledgment failed with a trace that marked changed inputs assessed using an old projection. This is bounded design evidence, not exhaustive or implementation verification.
