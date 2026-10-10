# Petri task workflow plan

Project: **Petri**. Priority: **2**. Date: 2026-10-09. Parent task: `SKYBUILD-PETRI`.

Source revision: `8890adb07930e3ce47d8075ed6da86b9f962a8c8`. Governing design: architecture A42, section 5.

This document contains the approved design and implementation plan. Implementation has started under priority 2.
Task history records actual progress and acceptance. This document does not attest completion.
The task API stores the current task records. PostgreSQL stores the authoritative task state and history.

Use short sentences and consistent technical names. Use full place names in all descriptions, tables and diagrams.
This wording follows the approach of [ASD-STE100 Simplified Technical English](https://www.asd-ste100.org/about_STE.html).

## Purpose

Add a small Python component to track tasks for SkyBuild and other projects.
A task is one unit of work. A worker, an attempt and a bundle are separate objects.
The pair `(project_id, task_id)` identifies the task. This identity does not change.

Each task has one current token. The token occupies exactly one place.

| Place | Meaning |
| --- | --- |
| Ready | The task waits for permitted work. Dependencies can prevent a start. |
| Working | A worker owns the task and does the work. |
| Validating | Required checks, review or base assessment remain in progress. |
| Integrating | A bundle gate, publication or final acceptance remains in progress. |
| Done | Current evidence satisfies the required acceptance. |
| Deferred | The owner postpones the task until a date, milestone or explicit resume action. |
| Hold | The task waits for an explicit release, missing definition or unresolved condition. |

The normal path is Ready, Working, Validating, Integrating, Done.
Every confirmed failure in Validating or Integrating returns the task to Ready.
The token keeps the fault and the next action.

Create a task in Ready if its definition is sufficient. Otherwise, create the task in Hold with a named blocker.
A dependency can prevent work while the task remains in Ready.
A reopened Done task returns to Ready.

A superseded task remains in Hold with its replacement links.
Superseded means that another task replaces its scope. Superseded does not mean Done.
Do not add another place for review, rebase, failure or supersession.

## Technical terms

| Term | Meaning in this plan |
| --- | --- |
| Token | The current task record that moves between places. |
| Transition | A permitted movement or update of a token. |
| Guard | A condition that must be true before a transition. |
| Evidence | A result that identifies its inputs, producer and related artifacts. |
| Current evidence | Evidence whose inputs still match the required task inputs. |
| Stale evidence | Evidence whose inputs no longer match the required task inputs. |
| Input generation | A counter that changes when relevant task inputs change. |
| Claim fence | A version that prevents an old worker from changing current work. |
| Effect | An external operation that can continue after a request or connection stops. |
| Reassessment | A check of current requirements, evidence and permitted next actions. |
| Idempotency key | An operation identifier that prevents a repeated request from applying the operation twice. |
| Combined gate | The required checks for one fixed bundle candidate. |

This design uses a colored Petri net with control and retry loops.
It also has the form of a guarded state machine because each task has one token.
This plan does not claim formal WF-net soundness.
Check token conservation, guards and recovery instead.

## Library decision

The research date is 2026-10-09.

| Library | Relevant function | Decision |
| --- | --- | --- |
| [SNAKES](https://snakes.ibisc.univ-evry.fr/) | Uses Python objects as tokens and supports guarded transitions. | The best library match for this token model. Keep it optional for design experiments. |
| [SimPN](https://bpogroup.github.io/simpn/) | Simulates timed colored Petri nets. | Consider it later for throughput simulation. |
| [PM4Py](https://github.com/process-intelligence-solutions/pm4py) | Analyzes process logs and Petri nets. | Consider it later for task-history analysis. |
| [CPN-Py](https://pypi.org/project/cpnpy/) | Models colored nets and their state spaces. | No required advantage for this seven-place component. |

Use a small transition table for the first implementation.
SNAKES does not replace task transactions, claims, evidence checks or recovery rules.
Its initial use would add a second representation without removing those requirements.

The [SNAKES package record](https://pypi.org/project/SNAKES/) lists version 0.9.33 from 2024-06-03.
Upstream specifies LGPL-2.1-or-later. Its archived GitHub repository points to [Codeberg](https://codeberg.org/fpom/snakes).
This design pass did not test compatibility with SkyBuild's Python.

If future transitions must combine several tokens, assess SNAKES again.
Keep the task storage and caller interfaces unchanged.
Do not add a general net framework, expression language or plugin system.
Never evaluate Python expressions supplied by users.

## Small class design

Start with four classes in the existing `workflow.py` module.
Add enums for places, validation stages and result states.
Split the module only when its responsibilities require a separate module.

| Class | Responsibility |
| --- | --- |
| `TaskToken` | Stores the task identity, place, required attributes and evidence references. |
| `ValidationResult` | Stores one stage result, its inputs, producer, findings and artifact references. |
| `TransitionSpec` | Defines an event, permitted source places, destination rule and named guard. |
| `TaskWorkflow` | Provides `enabled(token, context)` and `apply(token, event, context)` without external operations. |

Use immutable dataclasses for the three record classes.
Use bounded, typed API dictionaries for events.
Use existing project configuration for versioned check requirements.
Use existing API serializers for the board response.
Do not add separate service, policy or projection class hierarchies.

One transition table supplies execution rules, available actions, the diagram and test cases.
Existing Store methods remain the transaction boundary.
Commit the task change, journal event and evidence invalidation together.
Use the existing task record. Do not create a second authoritative token table.

## Token contents and validation

The token contains these values or stable references:

- The task number, project, title, repository links and priority.
- The source branch, source head, target base and definition revision.
- The input generation and policy version.
- The requirements, dependencies, evidence, findings and faults.
- The next action, responsible party and blocker.
- The hold reason, deferral trigger, attempt, claim fence and bundle.

Keep large logs in existing artifact storage. Keep references to those logs in the token.
Apply the existing API limits to all fields.

Validation has five stages: unit tests, scans, long tests, code review and needs rebase.
Each stage has a result state: pending, running, passed, failed, stale or not applicable.
These result states are not places.
Independent checks can operate at the same time. They update the same task token.

Each result identifies the task, attempt, inputs, producer and artifacts.
Include the source head, target base, input generation and applicable claim fence.
Include the check identity, parameters and tool version.
Accept a result only from a permitted producer.

A task can leave Validating only when all required evidence is current and satisfactory.
All blocking review findings must have accepted dispositions.
A not-applicable result needs a policy reason.
The deferred complexity gates do not become MVP requirements through this change.

A base assessment can report that no rebase is necessary.
If a rebase is necessary, return the task to Ready with that fault.
Perform the rebase in Working. Submit the new head to Validating.
Invalidate affected tests and reviews after the head or base changes.
Keep old results in history.

A task that does not require publication still passes through Integrating.
Integrating records the policy reason and checks acceptance without inventing a merge.

Ready can retain current evidence. Do not skip Working or Validating to reach Integrating.
A worker can perform a bounded correction or reassessment without changing source code.
Failures do not reset spending, retry limits, findings or task lineage.

## Transitions

The following table defines the initial permitted transitions.
Reject source and destination pairs that the table does not permit.
Task numbers in the last column identify implementation coverage.

| Event | From | To | Guard / result | Slice |
| --- | --- | --- | --- | --- |
| Create | No token | Ready or Hold | Require a sufficient definition for Ready. Otherwise, name the blocker in Hold. | 01, 04 |
| Admit and claim work | Ready | Working | Require current inputs, satisfied dependencies, permitted effects and a live exclusive claim. | 02, 04, 06 |
| Submit work | Working | Validating | Require the current permitted attempt and evidence for the exact inputs. | 02, 06 |
| Record validation progress/pass | Validating | Validating | Accept one current result per operation. Preserve results from independent checks. | 03, 07 |
| Validation failure / rebase needed | Validating | Ready | Record the failed requirement or finding. Mark affected evidence stale. Keep the fault. | 03, 07 |
| Freeze bundle / accept integration work | Validating | Integrating | Require current validation and fixed bundle members, heads, base and policy. | 02, 08 |
| Integration progress / publication unknown | Integrating | Integrating | Keep the stored attempt and publication intent. An unknown outcome is not a confirmed failure. | 08, 09 |
| Integration failure / rebase needed | Integrating | Ready | Require a confirmed failure. Keep the fault. Resolve external effects before new work. | 03, 08 |
| Exclude before publication | Integrating | Validating | Confirm exclusion before publication dispatch. Reassess current evidence. | 08, 09 |
| Confirm acceptance | Integrating | Done | Verify exact publication and task inclusion, or apply the explicit acceptance policy without publication. | 08 |
| Work failure / safely abandoned work | Working | Ready | Record the failure or correction. Fence and resolve active effects. | 03, 06 |
| Hold | Ready, Working, Validating, Integrating, Deferred | Hold | Keep the reason, responsible party and interrupted place. Resolve effects or leave the request pending. | 03, 09 |
| Release hold | Hold | Ready | Require explicit release and resolved effects. Reassess inputs. Do not restore execution approval. | 03, 09 |
| Defer | Ready, Working, Validating, Integrating, Hold | Deferred | Keep the reason and a date with time-zone offset or milestone. Resolve active effects. | 03, 09 |
| Resume deferred | Deferred | Ready | Require a due trigger or explicit owner action. Reassess permissions and dependencies. | 03, 09 |
| Reopen | Done | Ready | Require a reason. Preserve accepted history. Invalidate affected evidence and dependent readiness. | 03, 09 |
| Edit / dependency / policy invalidation | Ready, Working, Validating, Integrating, Done, Deferred, Hold | Same place initially | Increase the affected input generation. Append history. Resolve effects before corrective movement. | 04, 09 |
| Reassess stale live work | Working, Validating, Integrating | Ready | Require new work and resolved old effects. Otherwise, show the pending condition. | 09 |
| Reassess completed acceptance | Done | Ready or Done | Reopen only if acceptance changes materially. Keep the publication history. | 09 |
| Hold/defer detail change | Hold or Deferred | Same place | Check the expected revision before changing the reason, responsible party or trigger. | 03 |
| Split/merge retirement | Ready, Working, Validating, Integrating, Deferred, Hold | Hold | Resolve active effects. Retire the old scope and create linked replacements in one transaction. | 09 |


A superseded token cannot start, resume, release or defer.
First record a structural correction that restores the task's active disposition.
This rule also prevents release through Hold, Deferred and Ready.
Reopen Done before a hold or deferral request.
An edit in Hold or Deferred preserves that explicit owner choice.

A hold or deferral request can remain pending while an effect is unresolved.
A pending request prevents new effect dispatch and bundle selection.
Retain terminal external results while the request is pending.
Unknown publication remains Integrating until the actual outcome is known.

Record stale results against their original attempts. Do not let stale results advance the current task.
Return the original outcome for an exact repeated request.
Reject a repeated key with a changed payload.
Reject stale revisions, wrong project identities and unauthorized producers without partial changes.

If a check failure and a check pass arrive together, the failure leaves the task in Ready.
A late pass cannot restore Validating.
Preserve unresolved effect ownership even when a confirmed fault returns the task to Ready.

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

Circles show places. Rectangles show transitions.
The table also defines control actions, reopening, exclusion and updates that do not change the place.

## Existing component connections

Use the components at the pinned source revision.
Check the current symbols again before each implementation task.

| Existing component | Required change |
| --- | --- |
| `workflow.py` | Add the shared transition rules. Preserve required manual actions and due-date checks. |
| `store.py` and `claims.py` | Combine transition, claim, journal and invalidation checks in existing transactions. |
| `api.py` and `client.py` | Expose guarded actions and computed capabilities. Preserve existing response fields. |
| `manual_dispatch.py`, `manual_assignment.py`, `manual_result.py` | Send assignments and results through the shared transition interface. |
| `admission.py`, `cpu_dispatch.py`, Store effect methods | Preserve permissions, resource limits, fences and unresolved effects. |
| `observations.py` and `scheduler.py` | Record results and reassess due tasks without starting a model. |
| `prepare_bundle.py` and `integrate_reviewed_pr.py` | Report fixed membership, checks, exclusions and confirmed publication. |

The existing claim operation and movement to Working are separate.
Petri must combine their acceptance in one transaction.
The result collector's ready-for-review result becomes Validating with code review pending.

Use one guarded mutation path for all producers.
Worker adapters consume permitted Ready work.
Check and review adapters consume Validating requirements.
Bundle adapters select eligible Validating tasks and move them to Integrating.

Cord messages contain task and event references. They do not contain another authoritative task state.
Reuse existing delivery and operation identities.
Do not create another database, event bus, scheduler or worker daemon.
A workbench refresh starts no work.

Keep old status and phase fields as compatibility outputs.
Add `place`, `validation`, `enabled_actions` and evidence freshness to the response.
Do not permit direct writes to computed place or evidence fields.
Use fake publisher responses until the trusted publication component is qualified.

## Dependencies and work selection

Store requirements and prerequisite task IDs on the task.
A completed prerequisite can enable dependent work. Its Done token remains in Done.
Reopening or changing a prerequisite invalidates affected dependent readiness.
Use the existing project graph lock to reject concurrent dependency cycles.
Initially, dependencies remain inside one project. Cross-project references are links only.

Show unmet dependencies inside Ready. Do not add a Blocked place.
Select permitted work before applying this order:

1. The existing priority, with lower numbers first.
2. The earliest Ready time.
3. The task ID.

Show each blocker and the direct tasks that a completion can unblock.
Do not add a transitive urgency score initially.
Add that score later only if the simple order fails to expose important prerequisites.
Never change owner priority or spending limits through a score.

Show the count and oldest age for each place.
Separate tasks that can start from tasks with unmet dependencies.
A large Validating count can show a check or review limit.
A large Integrating count can show a gate or publication limit.
Counts alone do not define importance or permit more capacity.

## Workbench

Show seven columns in this order: Ready, Working, Validating, Integrating, Done, Deferred, Hold.
Provide a list view and keyboard operation.
Each card shows its task, priority, next action, responsible party, age, links, dependencies and fault.

Validating cards show the five stages and evidence freshness.
Integrating cards show the bundle, gate and publication state.
Hold and Deferred cards show their reason and release condition.
Done cards link accepted evidence.

Calculate counts for the complete filtered project, not only the current page.
Identify stale or unavailable information.
Task details show requirements, dependency links, evidence, findings and the ordered journal.
Show permitted actions and the reasons for disabled actions.

Use expected revisions and stable operation IDs for edits.
If an edit conflicts, reload the current task and explain the difference.
Do not silently repeat a different owner decision.
Display titles, findings and links safely.
Preserve the existing authentication boundary.

## Implementation tasks

The API contains `SKYBUILD-PETRI-01` through `SKYBUILD-PETRI-12`.
All tasks have priority 2 and a responsible owner.
The parent waits for task 12. No child waits for parent completion.
The task table references the API records. It is not another editable task queue.

The owner authorizes implementation now. Aim for approximately 30 minutes of focused work per task.
If a task needs more time, record a checkpoint or split at an interface.
Tasks 04, 08, 09 and 12 can exceed this target.
Each code task needs applicable checks and an independent review in a separate qualified session.

| Task | Required work and checks | Prerequisite tasks | Author | Generation time | Reviewer |
| --- | --- | --- | --- | --- | --- |
| 01 | Add four classes and the enums. Check task identity and JSON conversion. | None | Luna | 10–15 min | Sol |
| 02 | Add the transition table and normal path. Reject movements that the table does not permit. | 01 | Luna | 15–20 min | Sol |
| 03 | Add validation stages, failure recovery, Hold, Deferred and reopening. Check each permitted source place. | 02 | Sol | 20–30 min | Sol |
| 04 | Add the Store transaction and migration. Combine the claim and movement to Working in one transaction. | 02 | Sol | 25–40 min | Sol |
| 05 | Adapt the API and client. Check permissions, revisions, available actions and existing response fields. | 03, 04 | Sol | 15–25 min | Sol |
| 06 | Connect worker assignments and results. Reject old results and repeated assignments. | 05 | Sol | 15–25 min | Grok* |
| 07 | Connect checks, review and rebase results. Check all five stages and reject results for old inputs. | 05 | Sol | 20–30 min | Grok* |
| 08 | Connect bundles, gates and publication. Check failures, exclusions, uncertain results and accepted task inclusion. | 05 | Sol | 25–40 min | Sol |
| 09 | Adapt dependencies, due dates, reassessment, split and merge. Preserve history, ownership and unresolved effects. | 05 | Sol | 25–40 min | Sol |
| 10 | Add board queries and seven columns. Check project totals, stage badges and safe text display. | 05 | Luna | 20–30 min | Sol |
| 11 | Add workbench controls and task details. Check revisions, dependency links, the diagram and keyboard operation. | 09, 10 | Luna | 20–30 min | Sol |
| 12 | Check all transitions and concurrent changes. Test two projects and prepare the migration and promotion handoff. | 06, 07, 08, 09, 11 | Sol | 20–35 min | Sol |

Sol, Luna and Grok are model profile names supplied by the owner.
Pin the actual model version before assignment.
Use Luna for bounded records, fixed transitions and the workbench.
Use Sol for transactions, claims, evidence, adapters and recovery.
The author and reviewer must use separate sessions, including when both use Sol.

**Grok*** is the proposed reviewer for tasks 06 and 07.
Use Grok only if its route is qualified for those task classes.
Otherwise, use a separate qualified Sol session with the same time allowance.
These choices reflect task risk. They are not measured model-speed comparisons.

## Parallel generation

Dependencies identify required interfaces, not a preferred serial order.
Accept the prerequisite contract and pin its source revision before downstream generation.
Independent tasks can generate in separate worktrees.
Final integration still requires all relevant dependencies and reviews.

| Group | Tasks | Prerequisite |
| --- | --- | --- |
| 1 | 01 | Task selection. |
| 2 | 02 | Task 01. |
| 3 | **03 and 04 in parallel** | Task 02. |
| 4 | 05 | Tasks 03 and 04. |
| 5 | **06, 07, 08, 09 and 10 in parallel** | Task 05. |
| 6 | 11 | Tasks 09 and 10. Other group-5 tasks can continue. |
| 7 | 12 | Tasks 06, 07, 08, 09 and 11. |

The maximum planned width is five author sessions.
Actual capacity and permissions can reduce this width.
This schedule does not start workers or models.

~~~mermaid
flowchart LR
    Contracts[01 Token and result contracts] --> Catalogue[02 Transition and event interface]
    Catalogue --> Behavior[03 Validation and control behavior]
    Catalogue --> Persistence[04 Store and claim transaction]
    Behavior --> API[05 API and client]
    Persistence --> API
    API --> Workers[06 Worker adapters]
    API --> Validation[07 Check review and rebase adapters]
    API --> Integration[08 Bundle and publication adapters]
    API --> Reconciliation[09 Dependencies and reconciliation]
    API --> Board[10 Board and read queries]
    Reconciliation --> Controls[11 Workbench controls and detail]
    Board --> Controls
    Workers --> Acceptance[12 Combined acceptance]
    Validation --> Acceptance
    Integration --> Acceptance
    Reconciliation --> Acceptance
    Controls --> Acceptance
~~~

Task 02 fixes event names, payloads, signatures and journal facts.
Task 03 owns pure workflow behavior. Task 04 owns Store, claim and migration changes.
Task 05 tests their real combination before other adapters start.

Task 05 fixes the mutation path, result envelope, capabilities and response fields.
Tasks 06, 07 and 08 own separate producer adapters and tests.
Each adapter uses fake counterpart events at that shared interface.
The validation adapter does not need the worker implementation to generate its code.
The bundle adapter does not need the validation implementation to generate its code.
The final runtime sequence remains unchanged.

Task 09 owns dependency, due-date, structural Store and scheduler changes.
Task 10 owns read-only board queries and board display.
Task 10 uses the existing dependency graph and the fixed response fields.
Task 11 waits for both tasks before adding controls and details.

Assign one owner to each shared file or symbol.
Keep migration numbering and shared mutation interfaces with their named owners.
If a missing interface needs a shared edit, update the dependency graph before dispatch.
Task 12 checks the exact reviewed heads together.

## Time estimates

Code generation includes input inspection, source and test generation, and the first edit pass.
It excludes model-slot waits, test processes, review, corrections and integration.
The estimates are planning judgments. They are not observed model performance.

| Work | Estimate | Basis |
| --- | --- | --- |
| Luna code generation | 65–95 min | Tasks 01, 02, 10 and 11. |
| Sol code generation | 165–265 min | The other eight tasks. |
| Independent review | 96–180 min | 8–15 minutes per task. |
| Targeted tests and result inspection | 60–120 min | 5–10 minutes per task. |
| Corrections | 60–90 min | Approximately 25% of generation time. Re-estimate further correction rounds. |
| Final combined gate and bundle handling | 60–120 min | One combined candidate. Use the actual required suite duration when known. |
| **Total serial build budget** | **506–870 min, approximately 8.5–14.5 hours** | Excludes slot waits, missing components and production promotion. |

Total generation work is **230–360 model-minutes**, or **3 hours 50 minutes to 6 hours**.
With sufficient model slots, the generation-only critical path is **130–205 minutes**.
This is **2 hours 10 minutes to 3 hours 25 minutes**.
The path is 01, 02, 04, 05, 09, 11, 12.
Task 10 also gates task 11, but does not increase this range.

The critical-path estimate excludes reviews, tests, corrections, slot waits, shared edits and integration.
Do not divide the full serial estimate by five.
Use 8.5–14.5 hours as the conservative build budget until actual results are available.
Re-estimate after tasks 01–03 from the recorded generation, review and gate times.

Integrate reviewed tasks in dependency order through existing fixed bundles.
Keep the existing limit of 20 task IDs per bundle.
Do not create individual task PRs.
Name any missing component interface before expanding a task.

## Simplicity and insertion assessment

The existing task IDs, database, journal, dependency graph and revision checks already supply most infrastructure.
The first useful change is the small kernel and compatible API in tasks 01–05.
Tasks 03 and 04 can proceed together.
Tasks 06–10 can then proceed together. Task 11 follows tasks 09 and 10.
Task 12 verifies the combined result.

The easiest changes are the place names, transition table and bounded records.
Adapter changes and old-phase migration need moderate effort.
The hardest changes combine claims, simultaneous validation results and uncertain publication.
Preserve existing controls for those operations.

The design uses four classes, one table and existing Store/API interfaces.
It needs no new database, scheduler, event bus or general policy framework.
A Petri-net library does not remove the difficult transaction and recovery requirements.

Confirm the actual old-phase inventory, project check profile and missing component interfaces before coding.
Do not migrate live tasks after completing only the kernel.

## Migration and final checks

Rehearse the migration in disposable PostgreSQL with a captured task snapshot.
Use the next migration number and a workflow schema version.
Keep all old journal events unchanged.

| Existing task condition | New place |
| --- | --- |
| Accepted completion | Done |
| Explicit deferral | Deferred |
| Ready work | Ready |
| Active author work | Working |
| Required validation, review or base assessment | Validating |
| Active bundle or publication | Integrating |
| Known correction or required rebase | Ready, with the fault |
| Incomplete definition or explicit hold | Hold, with the blocker |
| Superseded scope | Hold, with its disposition and replacement links |
| Unknown or ambiguous phase | Hold, with a named diagnostic owner and next action |

Use this mapping only after checking the actual records and their evidence.
Do not infer acceptance from a phase name alone.

Check every permitted transition and every rejected source/destination pair.
Test all five validation failures, integration failures, rebase, interruption, holds, deferrals and reopening.
Check stale evidence, duplicate requests, changed payloads, conflicting revisions and unauthorized results.
Confirm that failure recovery preserves faults, history, spending and unresolved effects.

Test two claimers, simultaneous check pass/fail, edits during review and concurrent dependency changes.
Test invalidation during acknowledgment, crashes around commit and retries after a lost response.
Confirm that uncertain publication prevents another attempt.
Reuse existing reassessment and publication models.
Add a bounded TLC model only if implementation changes their concurrency assumptions.
Use a broken variant to confirm that the model exposes the intended failure.
Model checking does not prove the database or remote API implementation.

Test SkyBuild and a second test project with a small fake repository adapter.
Demonstrate normal completion, rework, dependency release, hold, missed deferral triggers and acceptance without publication.
Reject unauthorized cross-project reads and writes.
Check board totals beyond one page, evidence freshness, disabled actions, safe display and keyboard operation.

Record source, schema and policy versions in the final handoff.
Include reviewed heads, gate evidence, API task IDs and remaining promotion controls.
Keep the accepted live service unchanged until separate promotion checks pass.
Do not mark Petri complete from this design or the kernel alone.
