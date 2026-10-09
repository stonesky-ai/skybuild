# Petri: compact task workflow design (priority 5)

Plan dated 2026-10-09. Source baseline: `8890adb07930e3ce47d8075ed6da86b9f962a8c8`, architecture A41. Design revision: A42. Project name: **Petri**. Priority: **5**. Parent task: `SKYBUILD-PETRI`. This document delivers design only, including a simplicity review and a proposed implementation backlog. It specifies the owner-requested design and the proposed implementation mechanics. It does not claim that the subsystem, component wiring, or workbench is implemented. Architecture section 5 governs; the API remains the only task authority. Task descriptions and current execution status live in the API, not in this plan.

## Outcome and scope

Build a small Python subsystem shared by SkyBuild and every enrolled project. A task is one work unit with immutable `(project_id, task_id)` identity, not a worker, process, attempt, or bundle. Exactly one current task token occupies exactly one of seven places: **Ready, Working, Validating, Integrating, Done, Deferred, Hold**. The normal path is Ready, Working, Validating, Integrating, Done. All confirmed failures in Validating or Integrating return to Ready with the specific fault and next action. Review, rebase, blocked prerequisites, and test progress do not create additional places.

Creation enters Ready when the definition is sufficient; otherwise it enters Hold with a definition blocker. Dependencies and admission can disable a token's start transition while it remains in Ready. Hold means explicitly suspended or awaiting reconciliation; Deferred means intentionally postponed with a date or milestone trigger. Done means accepted completion. Supersession is a disposition on a retained Hold token, not an eighth place and not accepted completion. Reopening Done explicitly returns to Ready.

This is a colored task net with control and retry loops. It is not a claim of classical WF-net soundness: creation, reopening, holds, and external publication violate assumptions of a simple single-source/single-sink workflow net. Verify task conservation, transition guards, and recovery instead. Validation workers may run independent checks concurrently; they update evidence on the same task token and never clone the task into several places.

## Research and library choice

Research checked upstream documentation and package metadata on 2026-10-09.

| Option | Relevant capability | Fit for Petri |
| --- | --- | --- |
| [SNAKES](https://snakes.ibisc.univ-evry.fr/) | Python objects as tokens, expressions as guards/arcs, executable colored nets, marking/state graph APIs. LGPL-2.1-or-later according to upstream. [PyPI](https://pypi.org/project/SNAKES/) reports 0.9.33, released 2024-06-03. The archived [GitHub repository](https://github.com/fpom/snakes) points to [Codeberg](https://codeberg.org/fpom/snakes). | Best library fit for colored tokens; optional modeling aid initially. Runtime adoption needs demonstrated benefit and Python compatibility qualification. |
| [SimPN](https://bpogroup.github.io/simpn/) | Timed colored Petri nets for discrete-event simulation. [Package metadata](https://pypi.org/project/simpn/) reports 1.10.0, Python >=3.9, MIT license. | Useful for later throughput experiments; simulated time and automatic event selection are not the task authority or admission policy we need. |
| [PM4Py](https://github.com/process-intelligence-solutions/pm4py) | Process discovery, conformance, and Petri-net analysis; AGPL-3.0 with separate commercial licensing. | Potential later analysis of exported journal events. Larger process-mining scope than this runtime subsystem requires. |
| [CPN-Py](https://pypi.org/project/cpnpy/) | Color sets, timed tokens, hierarchy, state-space analysis; package metadata reports GPL-3.0. | Interesting modeling alternative, but no demonstrated runtime advantage for seven places and existing PostgreSQL authority. |

Recommendation after simplicity review: use a small, table-driven colored task-net kernel in production. Keep SNAKES as an optional development reference for topology/reachability experiments, not an initial runtime dependency. With one token per task, seven places, and no token joins or resource places, the task lifecycle is also a guarded state machine. SNAKES fits the token model but does not remove the application guards, persistence, fencing, or compatibility work. Adding an ephemeral net to every Store transaction would duplicate those mechanics without a demonstrated benefit.

Use one transition specification for execution, API capabilities, the diagram, and conformance fixtures. Fixed Python predicates evaluate domain guards. Never evaluate user-provided Python expressions. Do not build a general Petri-net framework, expression language, plugin system, XML/pickle persistence, or second scheduler. If future requirements actually need synchronization across multiple tokens, qualify pinned SNAKES on the project's Python, then replace only the transition evaluator. The database contract and callers remain unchanged. SNAKES compatibility has not been tested in this design pass; its package age alone is not evidence that it fails.

## Token and evidence contract

The token carries task identity/number, title, project/repository links, branch/head/base, definition revision, input generation, policy version, priority, requirements, dependency references, next action, responsible party, fault/blocker references, hold/defer details, attempt/fence and bundle references. Large logs and full immutable evidence remain linked records, not duplicated token blobs. Bounds follow the existing API limits.

Validation has five named substages: **unit tests, scans, long tests, code review, needs rebase**. The token carries each required substage's pending/running/passed/failed/stale/not-applicable state and immutable evidence references. These are evidence states, not Petri places. Needs rebase is a conditional work requirement: a clean base assessment satisfies it as not required; an actual rebase request is a fault returning Validating or Integrating to Ready. The rebase worker performs its correction while Working, then submits the new head to Validating. A no-integration project acceptance profile still follows Validating, Integrating, Done: Integrating records the explicit not-applicable integration decision and verifies acceptance without fabricating a merge.

Every result binds task, attempt, fence where applicable, head/base, definition/input generation, check identity/parameters/tool version and trusted producer. A policy may order checks, or run independent checks concurrently. A task cannot leave Validating until all applicable requirements are current and passed, no blocking review finding remains, and mergeability is assessed. An explicit not-applicable result needs a policy reason. A pending hold/defer request disables new effect dispatch and bundle freeze while existing effects reconcile; a terminal external fact is still retained. Existing scans may be required; deferred dedicated complexity gates do not become MVP prerequisites.

Keep historical passed stages after failures and edits, but mark affected evidence stale. A new source head invalidates head-bound tests and reviews by default; a new base invalidates affected merge/base checks. Reuse requires demonstrably equivalent inputs and trusted provenance. Ready may retain current passes, but no Ready-to-Integrating shortcut bypasses Working or Validating. A worker may perform a bounded no-code correction/reassessment when acceptance needs no source change. Failure does not reset spend, retry limits, findings, or lineage.

## Small class set and boundaries

Start with **four small classes**, plus place/substage/outcome enums. Do not add a class per state or component.

| Class | Responsibility |
| --- | --- |
| `TaskToken` immutable dataclass | Current identity, place, bounded attributes, requirements and evidence references. |
| `ValidationResult` immutable dataclass | Substage, outcome, exact inputs, producer, findings and artifact references. |
| `TransitionSpec` immutable dataclass | Event name, allowed source places, destination rule and named fixed guard. One catalogue shared by execution, capabilities and diagram. |
| `TaskWorkflow` | Pure `enabled(token, context)` and `apply(token, event, context)` methods. Returns proposed token and journal facts without input/output. |

Use existing Store methods as the transactional application boundary. Events are bounded typed input dictionaries using the existing API validation style; policies are existing project configuration with a version. Existing API serializers build the board projection. These do not need new service, policy, event, repository, or projection class hierarchies. Keep the kernel in one focused module initially; split only when a real responsibility or size warrants it.

The token carries references to existing tasks, evidence, attempts and bundles. Do not introduce a parallel authoritative token table: add the current place and required compact workflow fields to the existing task projection using the next numbered migration. Store still commits transition, journal and invalidation together. Dependency evaluation reuses the existing project graph and transaction lock. Component adapters call one Store transition path; they do not acquire their own transition tables.

## Transition catalogue and coverage

The following is the complete initial movement contract. Each row maps to an implementation task and positive/negative checks. All unnamed movements are rejected. Creation and metadata/evidence self-loops are explicit, so “cover every transition” does not mean permitting every pair of places.

| Event | From | To | Guard / result | Slice |
| --- | --- | --- | --- | --- |
| Create | No token | Ready or Hold | Definition sufficient, otherwise named definition blocker; one token only. | 01, 04 |
| Admit and claim work | Ready | Working | Current definition/dependencies, effect-specific admission, exclusive live ownership/fence. | 02, 04, 06 |
| Submit work | Working | Validating | Current authorized attempt, exact result inputs and immutable evidence. | 02, 06 |
| Record validation progress/pass | Validating | Validating | One current result per operation; preserve concurrent independent passes. | 03, 07 |
| Validation failure / rebase needed | Validating | Ready | Specific failed requirement/finding; invalidate affected evidence and retain fault. | 03, 07 |
| Freeze bundle / accept integration work | Validating | Integrating | All required validation current; exact frozen membership/head/base/policy. | 02, 08 |
| Integration progress / publication unknown | Integrating | Integrating | Durable attempt/intent; unknown is not a confirmed failure. | 08, 09 |
| Integration failure / rebase needed | Integrating | Ready | Confirmed failure, preserved fault; reconcile external exposure before new work. | 03, 08 |
| Exclude before publication | Integrating | Validating | Confirmed exclusion and no dispatched/unknown publication; reassess current evidence. | 08, 09 |
| Confirm acceptance | Integrating | Done | Trusted exact publication/task inclusion, or explicit no-integration acceptance policy. | 08 |
| Work failure / safely abandoned work | Working | Ready | Failure/correction recorded, active effects fenced and reconciled. | 03, 06 |
| Hold | Ready, Working, Validating, Integrating, Deferred | Hold | Reason/resolver and interrupted place retained; reconcile effects, otherwise request pending. | 03, 09 |
| Release hold | Hold | Ready | Explicit release, quiescent effects and reassessed inputs; no restored execution approval. | 03, 09 |
| Defer | Ready, Working, Validating, Integrating, Hold | Deferred | Reason, offset-aware date or milestone and safe reconciliation. | 03, 09 |
| Resume deferred | Deferred | Ready | Due trigger or explicit owner action; reassess admission/dependencies. | 03, 09 |
| Reopen | Done | Ready | Explicit reason, preserve accepted history, invalidate affected evidence/dependents. | 03, 09 |
| Edit / dependency / policy invalidation | Ready, Working, Validating, Integrating, Done, Deferred, Hold | Same place initially | Increment affected generation, append history; reconcile before corrective movement. | 04, 09 |
| Reassess stale live work | Working, Validating, Integrating | Ready | New work needed, old effects reconciled; otherwise remain visibly pending. | 09 |
| Reassess completed acceptance | Done | Ready or Done | Reopen only when acceptance is materially invalidated; retain historical publication. | 09 |
| Hold/defer detail change | Hold or Deferred | Same place | Revision-guarded reason/resolver/trigger update. | 03 |
| Split/merge retirement | Ready, Working, Validating, Integrating, Deferred, Hold | Hold | Quiescent, disposition superseded and replacement links; new tasks created Ready/Hold atomically. | 09 |

A superseded token cannot start, resume, release, or defer regardless of its place. A separately journaled structural correction must first restore its active disposition. This prevents Hold-to-Deferred-to-Ready from bypassing retirement. Done cannot be held/deferred directly; reopen first. Edit/reassess in Deferred or Hold preserves the explicit hold/deferral until released. Reassessment never silently skips to an old interrupted phase.

Stale results attach only to historical attempts. Exact duplicates return the original idempotent result. Conflicting duplicate payloads, stale expected revisions, wrong project, unauthorized producer and absent guards reject atomically. None of these creates a current movement. A failed check concurrent with another pass must leave Ready; the late pass cannot restore Validating. Outstanding execution or uncertain publication remains fenced even when a confirmed fault has returned its task to Ready.

~~~mermaid
flowchart LR
    Ready((Ready)) --> start[Admit and claim]
    start --> Working((Working))
    Working --> submit[Submit work]
    submit --> Validating((Validating))
    Validating --> freeze[Validation satisfied / freeze]
    freeze --> Integrating((Integrating))
    Integrating --> accept[Confirm acceptance]
    accept --> Done((Done))
    Validating --> vf[Validation fault / rebase]
    vf --> Ready
    Integrating --> inf[Integration fault / rebase]
    inf --> Ready
    Ready --> hold[Hold]
    hold --> Hold((Hold))
    Hold --> release[Release and reassess]
    release --> Ready
    Ready --> defer[Defer]
    defer --> Deferred((Deferred))
    Deferred --> resume[Resume and reassess]
    resume --> Ready
~~~

The diagram highlights normal delivery and recovery. The catalogue governs additional source places, self-loops, exclusion and reopening. Circles are places; rectangles are actions. Workers label actions in the workbench, never add places.

## Wire existing components

Inspected current source through this checkout's CodeGraph and scoped source reads. `store.py` currently owns task actions, dependency locking, idempotency, journaling and readiness generations. `claims.py` currently creates ownership separately and requires ready status; Petri must make Ready-to-Working and claim acceptance one transaction, rather than adding a racy claim-then-status write. `api.py` and `client.py` expose task actions; `manual_dispatch.py`, `manual_assignment.py` and `manual_result.py` pin manual assignments/results. The result collector currently uses ready-for-review routing, which becomes Validating with review pending. `workflow.py` already supplies manual action changes and due-deferral recognition. Extend it rather than introducing a competing kernel. `admission.py`, Store effect methods, `cpu_dispatch.py`, `observations.py`, `scheduler.py`, `scripts/prepare_bundle.py` and `scripts/integrate_reviewed_pr.py` provide existing control/delivery seams. A fully automatic trusted publisher is not assumed to exist; its adapter can use a fake interface until that separate component is qualified. Confirm exact symbols at each task's own source baseline; this inventory is not a claim that every desired adapter exists today.

Producers submit typed facts: worker start/result, check result, reviewer finding/pass, base assessment, bundle membership/gate, publication intent/result, owner hold/defer/resume, and due trigger. The service consumes those facts and commits transitions. Consumers use current token capabilities: workers consume enabled Ready work, checks/review consume Validating requirements, and integrators select eligible Validating tokens then occupy Integrating. Cord messages and wakeups contain task/event references, never independent copies of mutable task state. Reuse existing Cord/effect delivery and idempotency where a consumer notification is necessary; retries cannot launch a second attempt. Do not add an outbox unless a demonstrated gap requires it. A UI refresh reads projections and launches no work.

Maintain old status/phase response fields as a documented compatibility projection during migration, not as another writable authority. Add explicit `place`, `validation`, `enabled_actions` and freshness fields. Deny arbitrary writes to computed place/evidence. Existing guarded action routes may adapt to the new service; only add a typed transition route if needed. Do not introduce a second event bus, database, scheduler daemon, or generalized BPMN service.

## Dependencies and what to work on next

Requirements carry stable acceptance/check IDs and prerequisite task IDs. Initial dependencies stay within the existing project boundary. A prerequisite's accepted Done token enables dependent start without consuming the prerequisite token. Reopening, scope changes, or supersession invalidate dependent readiness durably. Use the existing project graph lock to reject concurrent cycles. Cross-project task references are visible links only until separately designed isolation/authority rules exist.

Ready means queued work, not necessarily runnable work. Show dependency-blocked Ready tokens separately within Ready and expose the exact unmet prerequisite. Preserve explicit Hold and Deferred choices. Initial work selection filters enabled/admitted actions, then orders by existing priority (lower number first), oldest-ready time, and task ID. Show direct blocked-dependent counts and exact blockers. Do not compute a new transitive score initially. If observation shows that important prerequisites are buried, add explained inherited urgency over the project DAG under a later bounded task; it must not change budgets or owner priorities. Do not invent duration-based critical-path estimates when durations are unknown.

Board counts alone measure backlog, not importance. Show counts, oldest age and runnable/blocked totals per place, plus “work next” recommendations explaining urgency and what each task unlocks. A large Validating pile highlights check/review capacity; a large Integrating pile highlights gate/publication capacity. These observations can guide existing admission/backpressure policy; they never launch or buy capacity themselves.

## Workbench acceptance

Use seven columns in the owner's order: Ready, Working, Validating, Integrating, Done, Deferred, Hold. Show project/task, priority, next action/resolver, age, links, dependency count and fault on each card. Validating cards show five substage badges with freshness; Integrating cards show bundle/gate/publication state. Hold/Deferred cards show reason and release/trigger. Done cards show accepted evidence. Provide accessible list view and keyboard actions. Counts must cover the whole filtered project result, not just the current paginated page; label stale/unavailable data honestly.

Task detail shows requirements, dependency links, what is blocked/unblocked, ordered journal, historical and current evidence, and enabled actions with disabled reasons. Generate diagram/capabilities from the transition contract so it cannot drift into the old many-place chart. Mutations use expected revisions and stable operation identities. A conflict reloads current state and explains the change; it never silently retries a different user decision. Render titles/findings/links safely and retain existing authentication boundaries.

## Migration and plan examination

Roll out domain contracts, Store persistence, compatible APIs, component adapters, then workbench. The live service remains on its accepted version until separately qualified promotion. Rehearse migration on disposable PostgreSQL using a captured task snapshot. New persistent projections get a workflow schema/version; retain all old journal entries unchanged. Map done to Done, deferred to Deferred, explicit blocked/triage/incomplete definitions to Hold, ready to Ready, working to Working, validating/review/rebase assessment to Validating, and active bundle/publication to Integrating. Legacy needs-rework/needs-rebase with a known correction maps to Ready with fault. Superseded maps to Hold plus disposition/lineage. Ambiguous/unknown phases map to Hold with a bounded resolver, never guessed readiness. Pin and test the actual observed phase inventory before migration. Separate broad status from place and reject incompatible new client writes.

## Delivery slices

Create parent `SKYBUILD-PETRI` and `SKYBUILD-PETRI-01` through `SKYBUILD-PETRI-12` in the existing SkyBuild task API, all at priority **5**, with `initiative: Petri` metadata. Petri names this feature project; enrolling another runtime/API project is unnecessary for planning it. Current task status belongs only in the API. The table below references deliverables and sequencing, not a second editable queue.

Each slice targets roughly 30 minutes of focused work. Estimated code generation totals **230–360 minutes (3 hours 50 minutes to 6 hours)**. Independent review, test execution, corrections and combined gates are additional. These are planning judgments, not measured model benchmarks or delivery promises. A slice that cannot reach its boundary checkpoints or splits explicitly. The parent is an umbrella; no child depends on parent completion. Give every task a named next owner/action and exact acceptance checks. No implementation, task-completion attestation, deployment, or worker launch occurs in this design pass.

| Slice | Deliverable and focused acceptance | Depends on slices | Author model | Code generation | Reviewer model |
| --- | --- | --- | --- | --- | --- |
| 01 | Four compact classes/enums and bounded token/result serialization; preserve task identity and JSON round-trip. | None | Luna | 10–15 min | Sol |
| 02 | Transition catalogue and Ready, Working, Validating, Integrating and Done kernel; reject every unnamed movement, expose disabled reasons. | 01 | Luna | 15–20 min | Sol |
| 03 | Validation substages, fault-to-Ready, hold/defer/resume and Done reopening; cover all legal source places and evidence invalidation. | 02 | Sol | 20–30 min | Sol |
| 04 | Store transaction, next numbered migration and claim-to-Working atomicity; journal/idempotency/fence/rollback checks. | 03 | Sol | 25–40 min | Sol |
| 05 | Existing API/client action adaptation and computed capabilities; compatible fields, auth and stale-edit checks. | 04 | Sol | 15–25 min | Sol |
| 06 | Worker/dispatcher/result wiring for Ready, Working and Validating, plus Working failure; reject stale/duplicate assignments and results. | 05 | Sol | 15–25 min | Grok* |
| 07 | Check/review/rebase result wiring; all five substages, independent review, exact inputs and late-pass-after-failure check. | 06 | Sol | 20–30 min | Grok* |
| 08 | Bundle freeze/exclusion/gate/publication wiring for Validating, Integrating and Done, plus Integrating failure; retain unknown publication and exact inclusion checks. | 07 | Sol | 25–40 min | Sol |
| 09 | Existing dependency/due/reconciliation/structural actions adapted; preserve generation, lineage, effects and hold/defer semantics. | 08 | Sol | 25–40 min | Sol |
| 10 | Board/count/next-work query and seven-column accessible workbench; global totals, badges and safe rendering. | 05, 09 | Luna | 20–30 min | Sol |
| 11 | Guarded workbench controls, dependency/evidence detail and shared-contract diagram; expected revisions and keyboard flow. | 10 | Luna | 20–30 min | Sol |
| 12 | Full transition/race coverage, two-project acceptance and migration/promotion handoff. | 11 | Sol | 20–35 min | Sol |

### Estimate assumptions and total build time

“Code generation” means active model time to inspect the bounded inputs, produce source/test changes and perform the first edit pass. It excludes waiting for model slots, test processes, independent review, correction rounds and integration. Sol, Luna and Grok are the owner's model/profile labels; pin the actual permitted model version at assignment. No token-per-second comparison or live qualification is claimed.

Use Luna for bounded records, a fixed transition catalogue and UI work against already-guarded APIs. Use Sol for evidence, transactions, claims, compatibility, worker results, integration and recovery. Reviewers always use a separate session, even when author and reviewer both say Sol. **Grok*** is a proposed independent reviewer for slices 06 and 07 only if its route is already qualified for those task classes; otherwise use a separate qualified Sol session with the same review allowance. Grok is not on the author critical path. These choices reflect task risk, not a measured speed ranking.

| Budget component | Estimate | Basis |
| --- | --- | --- |
| Code generation, Luna | 65–95 min | Slices 01, 02, 10 and 11. |
| Code generation, Sol | 165–265 min | Remaining eight slices. |
| Independent review | 96–180 min | 8–15 minutes per task; separate Sol/Grok sessions as listed. |
| Targeted test execution and inspection | 60–120 min | 5–10 minutes per task; no full combined gate per task. |
| Correction allowance | 60–90 min | Approximately 25% of initial generation; additional failures require re-estimation. |
| Final combined gate and bundle handling | 60–120 min | One combined candidate assumed; use actual required suite duration when known. |
| **Total serial build budget** | **506–870 min (about 8.5–14.5 hours)** | Excludes unavailable capacity, new missing components and production promotion. |

The current dependency chain is mostly serial, so extra author models do not divide this total by worker count. Some review and CPU tests can overlap later work after their prerequisite boundary is accepted, but the estimate does not assume those savings. Allow roughly one to two working days when qualified model slots and the existing component seams are available. Keep the earlier 30-minute slice target as a checkpoint rule; slices 04, 08, 09 and 12 may overrun it and should split at a real interface if needed. Re-estimate from actual generation, review and gate times after slices 01–03 instead of treating these ranges as observed performance.

Every code task gets applicable checks and separate independent exact-head review. Integrate eligible reviewed tasks using the existing frozen-bundle process, in dependency order, at most 20 tasks per bundle. No task-specific PR or new delivery machinery is required. Reuse accepted controls/components rather than blocking slices on unfinished umbrella tasks. If a required component seam is absent, name the missing interface and split only that actual gap.

## Simplicity and insertion review

**Easy:** the existing task ID, PostgreSQL authority, task journal, dependency DAG, expected revisions, idempotency keys, input generations and workbench already supply most infrastructure. The seven-place kernel and readable labels are small changes. Existing broad status remains a compatibility projection, not another lifecycle.

**Moderate:** adapting existing worker/check/review result writers to one transition function and migrating legacy phases. These are changes at existing seams, not new subsystems. Scope each adapter to its current accepted source and preserve protocol compatibility.

**Hardest:** claim-plus-start atomicity, concurrent validation results, and unknown publication. These concerns already exist in SkyBuild. Petri must preserve existing fences and generation rules rather than trying to solve them with a Petri-net package. A short task is not permission to remove their checks.

The initial draft had 30 slices, eight domain/service types, a SNAKES adapter, and an ephemeral executable net per transaction. That was more machinery than this task model warrants. This revision reduces the design to four classes, one transition table, existing Store/API seams and 12 slices. The optional library experiment is not a prerequisite. No new database, token authority, event bus, worker daemon, scheduler or policy framework is needed.

For quickest useful insertion, deliver slices 01–05 as the kernel and compatible API, then wire existing producers in 06–09 and expose them in 10–11. Do not migrate live tasks or claim finished wiring after the kernel alone. Slice 12 verifies the integrated behavior. Start priority ordering with existing explicit priority, unmet dependency display and ready age; add transitive urgency only if the simple ordering actually fails to surface useful work.

Open implementation choices are limited to the exact observed legacy-phase mapping, current project check profile, and any missing accepted component seams. Existing fixed guards and the shared catalogue are sufficient; no configurable guard language is planned.

## Required final evidence

Prove one token per task, exactly seven places, all catalogue movements and every rejected source/destination pair. Exercise each of five validation failures, integration gate failure, confirmed publication rejection, unknown publication, rebase, worker interruption, every legal hold/defer source and release, Done reopening, stale results, duplicate delivery, same-key changed payload, conflicting revisions, and unauthorized evidence. Verify retained faults and passes never fabricate current readiness.

Reuse existing reassessment/publication models. Add a bounded TLC model only if implementation changes their concurrency assumptions; require a broken variant to expose the intended race. Store tests cover two claimers, concurrent check pass/fail, scope edit during review, dependency cycle/reopen, invalidation during acknowledgment, crash before/after commit, retry after lost response, and uncertain publication blocking a new attempt. Model checking is not a proof of the database or remote API.

Use two enrolled test projects, including SkyBuild and a minimal fake repository adapter. Demonstrate happy path, failure/rework, dependency unlock, hold, deferral catch-up, and no-integration acceptance without cross-project reads or writes. Workbench tests verify actual global counts beyond one page, enabled/disabled actions, clear faults/staleness, escaping and keyboard flow. Final handoff names source/schema/policy versions, exact reviewed heads, combined gate evidence, API task IDs and remaining promotion controls. Do not mark Petri done from this plan or from domain classes alone.
