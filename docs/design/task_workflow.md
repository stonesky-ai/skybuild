# SkyBuild task workflow

Derived from architecture revision A33, 2026-10-08. [Architecture section 5](architecture.md#5-bootstrap-rest-and-task-contract) governs. The owner requires an explicit workflow, actionable status, task management and append-only history. Phase names and mechanics below are proposals; this is planning, not a running workflow engine.

## State model and flowchart

Keep the existing broad task status and a delivery phase, rather than separate task, seam, todo and job queues. A task may have many attempts and bundle memberships in history. Only its current definition and current evidence determine its next step. Admission independently checks whether that step may run. “Ready” is not a spend approval, and “code complete” is not “done.”

~~~mermaid
flowchart TD
    P[Planned] -->|Definition sufficient| R[Ready for work]
    P -->|Requested planning scan returns proposal| P
    R -->|Admitted and claimed| W[Working]
    W -->|Implementation result| V[Validating]
    V -->|Applicable required checks pass| Q[Reviewing]
    V -->|Diagnosed code failure| X[Needs rework]
    Q -->|Actionable fixes or tests requested| X
    X -->|Rework admitted| W
    Q -->|Required reviews pass| A[Assessing integration readiness]
    A -->|Current head and prerequisites ready| B[Ready for bundle]
    A -->|Base conflict or required rebase| C[Needs rebase]
    C -->|Rebase completed and evidence invalidated| V
    B -->|Fixed bundle selected| I[Integrating]
    I -->|Confirmed publication and acceptance| D[Done]
    A -->|Acceptance met without integration| D
    I -->|Diagnosed task correction| X
    I -->|Candidate or base changed| A
    I -->|Excluded with remaining evidence current| A
    I -->|Gate running or publication uncertain| I
    A -->|Missing prerequisite or unknown cause| K[Blocked with resolver]
    K -->|Blocker resolved| E[Reassess affected phase]
    U[Edit, reset, dependency or evidence change] --> E
    E -->|Incomplete definition| P
    E -->|Implementation needed| R
    E -->|Checks stale or missing| V
    E -->|Review stale or missing| Q
    E -->|Ready to assess integration| A
    E -->|Known blocker| K
    DF[Defer task] --> F[Deferred with trigger]
    F -->|Trigger reached or owner resumes| E
    D -->|Rework requested| X
    S[Split or merge] --> L[Replaced task retained as superseded]
    S --> N[Linked replacement or surviving task]
    N --> E
~~~

The diagram shows the normal path and recovery routes. The transition table below also covers actions applicable from several phases; those edges are not repeated on every node. One ordered transition function resolves an event and current evidence to one next phase/reason. A non-applicable action returns a conflict or a journaled no-op, never an unspecified state. A resolved blocker returns to the earliest affected unmet requirement, not blindly to implementation. Definitions, resources and authorization can block any active phase. Phase and blocker are separate fields, so a waiting integration task retains where it was interrupted.

## Action-to-state contract

| Action or event | Result and next responsibility |
| --- | --- |
| Create task | Planned with definition/acceptance and next owner action; append creation event. |
| Edit description, scope, definition of done or considerations | Conditional definition revision and journaled change; reassess affected phases/evidence. An active attempt enters change-pending reconciliation before incompatible replacement work starts. |
| Request optional model planning scan | Record a bounded pending attempt, then a proposal or named failure/blocker. Accepted definition stays unchanged until a separate explicit edit adopts proposed changes. |
| Mark definition sufficient / resume work | Reassess dependencies and admission; ready for work or named blocker, never an implicit model launch. |
| Work, test or review result | Record exact attempt/input/evidence. Advance only if current; code failure becomes rework, infrastructure/unknown failure gets a named diagnostic/resource blocker. |
| Mechanical check or adversarial review requests changes | Journal rule/metric or finding, exact input, affected line/symbol/contract, severity and required fix/test. Needs-rework with responsible author; after correction, rerun affected checks and independent review. |
| Review findings resolved | Append dispositions and confirming evidence; advance only with current mechanical checks, an independent review pass and no unresolved blocking finding. Budget or reviewer shortage leaves a named review blocker. |
| Review policy changes | Journal a new owner/admin policy version; invalidate affected acceptance and reassess. Candidate suppressions or a status edit cannot bypass the trusted policy. |
| Request rework | Record reason and failed/changed criteria; return to needs-rework after reconciling active effects. Preserve previous completion/merge evidence if reopening a done task. |
| Rebase required/completed | Needs-rebase, then invalidate affected head/base evidence and return to the earliest unmet check/review. Record old/new refs and result. |
| Include in or exclude from bundle | Record bundle ID, member head and reason on every member task; integrating or reassess, respectively. Exclusion is not task completion. |
| Integration gate or publication result | Running/pending remains integrating with expected result/deadline. Failure records a specific next diagnostic/rework/rebase action. Only verified publication plus current acceptance produces done. |
| Set/change dependency | Validate project scope and cycles atomically; record old/new edge and invalidate affected dependents. Blocked while unmet, otherwise reassess. |
| Defer until date or milestone | Deferred with timezone-aware date or stable milestone reference, reason and resume condition. Reconcile active execution; do not label an unresolved effect stopped. |
| Deferral trigger becomes true / owner resumes | Reassess current definition/dependencies/authority. The trigger does not renew approval or guarantee readiness. |
| Split task | Create linked child/replacement IDs with explicit scope, acceptance and dependency mapping; retain original with superseded disposition when the split activates. Each replacement is reassessed. |
| Merge tasks | Select a surviving/new task and reconcile its definition, dependency edges and budget lineage. Retain replaced IDs as superseded with links; reassess survivor. |
| Re-evaluate/reset assessment | Append request/reason, increment affected input generation and schedule its responsible evaluator. Preserve code, history, spend, failure counts and unresolved attempts. |
| Stale result, duplicate event or retry | Attach new stale evidence to its original attempt, or return the original idempotent result. Current phase cannot advance on stale input; repeated delivery does not create another transition. |
| Unsupported action or stale user edit | Reject with current revision/state and a useful reason. Do not partially mutate task, graph or journal. |

For any unfinished task, the projection contains phase, next action or awaited event, responsible component/person, reason, blocker references, input generation and applicable next-check/deadline. Unassigned manual tasks default to the project owner as the next-action resolver, without claiming a worker. Deferred tasks have a revisit trigger; superseded tasks have replacement links. Unknown is allowed only with a bounded diagnostic/resolver path. The UI highlights overdue or ownerless transitions. A scheduler tick alone never wakes a model. Skip phases that the configured task acceptance does not require; CPU/planning tasks do not need an artificial code-merge stage.

## Task workbench

The page lists outstanding tasks with filters for project, phase, blocked/deferred state and owner. Each row shows a plain-language next step, blocking reason/age, evidence freshness and active bundle/attempt. Detail view presents editable description, scope, definition of done and considerations alongside immutable journal history. Provide actions for rework, reassessment, split, merge, dependencies and date/milestone deferral. Show the effect on existing evidence and active work before a structural change is submitted; this is the normal edit interaction, not another spend-approval layer.

Use expected revisions for edits. Split/merge changes commit their records, lineage, dependency mapping and events together under the existing graph concurrency rule. Preview ambiguous acceptance/dependency allocation for the owner. Do not silently drop incoming dependencies or merge completed and incomplete acceptance into “done.” Preserve consumed/uncertain usage across lineage; a split cannot turn an over-threshold task into fresh exempt budgets. Reconcile live or unknown effects before admitting replacement work. A pending remote merge cannot be presumed cancelled by editing the page.

User actions invoke guarded transitions, not arbitrary writes to computed gate, review or integration-success fields. The page cannot manufacture readiness by changing a status label. Manual bootstrap completion still follows that task's configured acceptance and records its evidence.

A milestone initially references a designated task's accepted completion or an explicit owner-recorded milestone event. No roadmap service is required. Preserve timezone and trigger semantics; laptop downtime results in catch-up reassessment on return, not fabricated past execution. If a milestone is subsequently reopened, reassess dependent readiness and record the change.

The optional planning scan first uses available CPU/source evidence, then bounded qualified inference to propose considerations, missing decisions, risks, reuse, decomposition/dependencies and cost estimates. Store input revision, model/profile, proposal and usage as linked journal/attempt evidence. A stale scan remains a proposal for its original definition. Scan availability cannot block ordinary manual task management; saving an edit or refreshing the page does not itself generate inference.

For MVP code tasks, show the separate reviewer session, findings and their disposition. Dedicated complexity/size measurements and GUI policy controls are deferred until the self-building MVP is running. A finding links exact code or a named contract, failure evidence and a proposed correction/test with expected outcome. Blocking findings return to the author; corrected inputs return through affected checks and independent review. Findings are never silently deleted when lines move or reviewers disagree. When the deferred quality feature is enabled, owner/admin settings and exceptions are versioned and affected readiness is recomputed. Its metrics and settings are not prerequisites for the MVP review path. See [review policy](review_policy.md). Multi-person approval chains remain deferred; independent model review is core.

## Immutable history and reliable reassessment

Journal events are append-only. There is no journal-edit/delete API; the runtime database role has no UPDATE/DELETE authority over journal rows, including through cascades. Administrative corrections append a reasoned correction/supersession event. ADR changes cannot rewrite history. Preserve original attribution and old definition/evidence when scope changes. A materialized current projection may change or be rebuilt without editing the journal. Version event schemas and retain old event interpretation; evolving the schema must not rewrite the historical facts.

Retain changed field values or an immutable recoverable definition snapshot, not merely a pointer to the mutable current task. Order a task's events by its transactionally committed revision/sequence; wall-clock timestamps and globally allocated IDs are not commit-order guarantees. Use stable event/operation identity so retrying one edit cannot append a second accepted transition.

One Store transaction commits the state change, journal event and durable invalidation marker. Dependency and bundle mutations also invalidate affected members/dependents durably. The CPU reconciler evaluates a captured generation; if inputs change, it must not clear the newer marker or publish a fresh-looking stale projection. Coalesce repeated signals, claim evaluations safely, retain attempts and retry with bounded backoff. Unknown external effects remain held while their outcome is reconciled. Do not rerun implementation merely because readiness is dirty.

Keep the actual task journal in the core PostgreSQL task/history contract. Periodic Git export and disaster-recovery reconstruction are separate later backup work. Large logs/artifacts have explicit completeness/retention state; an expired artifact reference remains in history as unavailable, not silently removed. Routine heartbeats and repeated unchanged scans are not task transitions and need not fill this journal.

## Delivery and validation boundary

Bootstrap stores journaled manual transitions and next-action metadata. SKYBUILD-TASK-WORKBENCH supplies the early page and structural operations after task authority cutover. Execution controls add automatic reconciliation; bundled integration links every member outcome. Qualified model scanning follows model admission readiness. No runtime implementation is authorized now.

Required future checks cover a scope edit during testing/merge, a new invalidation during evaluation, dependency cycles under concurrent edits, split/merge with running attempts, stale review/test results, bundle exclusion/retry, lost merge acknowledgment, duplicate events, missing evidence, deferral through laptop downtime, and attempted journal alteration. Each case must leave a deterministic state and named next step while preserving history and authority.

The bounded [reassessment model](models/Reassessment.md) exercises the lost-invalidation race. Its correct design passed sampled simulation; the deliberately broken acknowledgment produced the expected stale-projection counterexample. This does not verify the rest of the workflow or a database implementation.

Multi-person approval chains are explicitly deferred until much later under SKYBUILD-TEAM-APPROVAL-CHAIN. Stable actors and journal events preserve room for that feature; no approval-chain engine is required initially.
