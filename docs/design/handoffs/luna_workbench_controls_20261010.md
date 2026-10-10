# SkyBuild workbench and execution-control evidence

Date: 2026-10-10

Checkout: `dev-006` at published source commit `ec3fa9c12f560d205ddf46c08ccaef47012341c2`.

This handoff inventories source evidence for `SKYBUILD-TASK-WORKBENCH` and `SKYBUILD-EXECUTION-CONTROLS`. It does not change REST task state, attest completion, qualify a model profile, approve execution, or establish deployment state.

## Task Workbench acceptance matrix

| Criterion | Source evidence at `ec3fa9c` | Focused test evidence | Finding |
| --- | --- | --- | --- |
| Every action has a known next state or explicit conflict. | `src/skybuild/workflow.py`: `TRANSITIONS`, `_workflow_event`, `TaskToken`; `src/skybuild/store.py`: `workflow_transition`, `task_action`; `src/skybuild/api.py`: `POST /tasks/{task_id}/workflow` and guarded task-action routes. | `tests/test_workflow_transitions.py`: forbidden sources, changed/missing guards, stale revisions, direct movement, output receipt/fence and unknown publication. `tests/test_workflow_behavior.py`: pending control and unresolved-effect handling. | Implemented in source. REST authority and current eligibility still govern each operation. |
| Journal is append-only, with no edit/delete path, including by ADR. | `src/skybuild/store.py`: transactional journal writes and `task_history`; `src/skybuild/api.py`: history is read-only. The workbench has no journal-edit control. | `tests/test_workflow_transitions.py::test_journal_rejects_cross_task_or_nonsequential_changes`; `tests/test_store.py::test_journal_mutation_is_blocked_in_database`; `tests/test_web.py` checks public UI/security boundaries. | Implemented in source. Historical events remain API-owned. |
| Concurrent edits preserve task IDs, lineage, dependency correctness, prior evidence and history. | `src/skybuild/store.py`: `update_task`, `split_task`, `merge_tasks`, graph locks, dependency validation and lineage writes; `src/skybuild/api.py`: revision-bearing edits and structural routes; `src/skybuild/static/workbench.js`: previews the exact split/merge body and invalidates stale plans. | `tests/test_store.py::test_revision_history_restart_and_idempotency`, `test_split_rewires_explicit_dependencies_and_keeps_lineage`, `test_split_refuses_incomplete_map_and_cycle_without_partial_rows`, `test_merge_preserves_prerequisites_acceptance_and_history`, `test_merge_rejects_incomplete_map_cycle_and_dropped_acceptance`; `tests/test_web.py::test_browser_handlers_preserve_ids_and_preview_structural_mapping`. | Identity/history/lineage and dependency behavior have evidence. **Consumed or uncertain budget history does not.** Current task API/workflow records do not define a task-budget record or split/merge allocation contract. This remains blocked on execution-control accounting design and implementation. |
| Changed inputs invalidate readiness/evidence; late results cannot restore readiness. | `src/skybuild/store.py`: readiness generations and task revision checks; `src/skybuild/workflow.py`: result provenance and freshness predicates; `src/skybuild/reconciliation.py`: replacement-token handling. | `tests/test_workflow_behavior.py::test_stale_results_cannot_change_token`, `test_reopen_marks_evidence_stale_and_increments_generation`; `tests/test_petri_reconciliation_store.py::test_edit_preserves_hold_and_matches_actual_generation`, `test_reopen_invalidates_dependency_and_keeps_accepted_history`. | Implemented in source for modeled task/evidence inputs. |
| Live or unknown effects are reconciled before replacement work. | `src/skybuild/store.py`: guarded resume/release/rework and structural-change checks; `src/skybuild/workflow.py`: pending action/effect state; `src/skybuild/claims.py` and `src/skybuild/integration_workflow.py`: claim and publication reconciliation. | `tests/test_workflow_behavior.py::test_pending_control_preserves_ownership_and_finishes_after_effect_resolution`, `test_release_resume_reopen_require_effect_resolution`; `tests/test_petri_reconciliation_store.py::test_http_control_with_held_claim_stays_pending`, `test_resolved_started_scope_split_preserves_history_and_retires_to_hold`. | Implemented in source for represented claims/effects. A missing observation remains unknown; this is not a physical-stop assertion. |
| Date/milestone deferral reassesses without granting execution authority. | `src/skybuild/workflow.py`: `due_deferral`; `src/skybuild/store.py`: `reconcile_due_deferrals`; `src/skybuild/api.py`: bounded `POST /tasks/reconcile-due`; `src/skybuild/scheduler.py`: CPU catch-up. | `tests/test_workflow_behavior.py::test_due_resume_uses_trusted_trigger_without_owner_authority`; `tests/test_store.py::test_due_deferral_catch_up_and_reserved_state`, `test_due_reconciliation_pages_past_first_hundred`; `tests/test_petri_reconciliation_store.py::test_deferred_edit_preserves_trigger_and_due_resume_is_cpu_only`. | Implemented as bounded reassessment only; no model or worker launch is implied. |
| Basic task controls work with no model available. | `src/skybuild/static/workbench.html`, `workbench.js`; task edits, board reads, history, dependency and structural operations call task APIs directly. There is no planning-model call in this path. | `tests/test_web.py::test_browser_handlers_preserve_ids_and_preview_structural_mapping`; pure transition/store tests above. | Implemented in source. |
| Multi-person approval chains remain deferred. | `docs/design/architecture.md` section 5; `docs/design/implementation_plan.md` section 3a; `docs/adr/tasks.md` ADR 0031. The task workbench uses actor permissions and has no approval-chain control. | No multi-person approval test is required for deferred scope. | Correctly deferred. |

The current focused run used the checkout-local project runner on `test_web.py`, `test_workflow_behavior.py`, `test_workflow_records.py`, `test_workflow_transitions.py`, and `test_workflow_closeout.py`: **317 passed, 1 existing Starlette/httpx deprecation warning**. These were source tests only. They do not replace the independent exact-head review or disposable PostgreSQL/full combined gate in `docs/design/implementation/petri_acceptance_handoff.md`.

## Runtime and source boundary

The task branch is published `dev-006` commit `ec3fa9c12f560d205ddf46c08ccaef47012341c2`. The retained runtime source checkout `/home/kevin/my_code/skybuild-live-workbench-stack` remains unchanged at `b1975344fc28447188137996b6b0769a0ca7446b`; its `workbench.js` and `workbench.html` Git blob IDs match the `dev-006` files. The owner-provided live inventory says the rich UI is merged through PR 79 and the REST stack is on its accepted runtime revision; the backend Petri schema is accepted at migration 013. This source check does not independently attest the serving container's image, route response, or installed schema. Do not infer those from the UI bundle alone.

The older REST/API history around commit `9e41ac131afe3e4f44b85a99f9acd8f4d04178f7` predates the current `dev-006` file tree. Compare exact file blobs and runtime identity before using historical source as deployment evidence. The workbench source is present; the remaining acceptance gap is budget-history preservation plus independent runtime/combined-gate evidence, not another structural UI control.

The API task records remain the authority. The supplied REST snapshot has both task IDs at revision 9 in `reassess`/`blocked`, with stale dependency invalidation from `SKYBUILD-TASK-CUTOVER`. Repository source evidence does not release that blocker. A reviewed API operator must reassess through the owner-authorized workflow.

## Current REST closeout task and source slice

The owner-registered task is `SKYBUILD-MVP-ATTEMPT-CLOSEOUT` (r1, parent
`SKYBUILD-EXECUTION-CONTROLS`). Its acceptance criteria are:

1. Expiry denies new tasks; model calls stop after the immutable cutoff plus
   pinned grace.
2. Stop, clock discontinuity, unknown process and reconnect cannot silently
   clear restrictions or release uncertain exposure.
3. Idempotent durable journal, focused tests, bounded TLA+ model with mutation
   check, independent exact-head review and confirmed bundle publication are
   retained.

This source tranche implements the deterministic local reducer and journal in
`src/skybuild/attempt_closeout.py`, with focused cases in
`tests/test_attempt_closeout.py` and finite model/check configurations in
`docs/design/models/AttemptCloseout.tla`, `AttemptCloseout.cfg`, and
`AttemptCloseout-broken.cfg`. It composes a caller-supplied immutable attempt
pin rather than introducing a queue. It does not launch a process, claim that
a stop request physically stopped one, write API state, or qualify an
inference profile. The source tranche is not itself independent review or
confirmed bundle publication.

The journal pins project/task/attempt, claim fence, source and input digests,
approval ID, cutoff, outage policy, grace, invocation identity, and policy
version. Git SHA-1 and SHA-256 source IDs are accepted; the input digest is
SHA-256. The current-task outage policy defaults to the owner-selected
`finish-current-task`; the invocation and policy version must be explicit. The
grace defaults to 300 seconds and is fixed in that pin, with an explicit finite
bound. Policy methods deny new admissions and ordinary task calls at cutoff;
only bounded closeout calls are allowed during `[cutoff, cutoff + grace)`. All
model calls are denied at `cutoff + grace`. Contact loss before cutoff uses the
cached pinned `finish-current-task` or `checkpoint-stop` policy.

Every policy decision requires and durably records a current wall/monotonic/
boottime/boot-ID observation. A fresh process cannot make a decision from a
previously cached sample alone. Reboot, clock rollback/jump, monotonic
discontinuity, and suspend gaps park model calls until caller-authenticated
server reconciliation matches the unchanged pin; reconnect does not clear a
parked clock, operator stop, conflict, or process uncertainty. A process
observation must match the pinned invocation. Unknown/mismatched identity
retains exposure; terminal evidence for that invocation cannot be overwritten
by a delayed running/unknown sample.
Checkpoint and resume summaries carry only bounded text and opaque artifact
references tied to the pinned task. Authenticated exit reconciliation is
required before exposure can be released.

The local journal uses a process lock, mode-0700 directory, mode-0600 record,
append-only hash-chained events, canonical bounded JSON, fsync of the temporary
file, atomic rename, and directory fsync. Same-operation identical replay is
idempotent; changed payload, stale expected sequence, wrong pin, corrupt chain,
or out-of-order restriction-changing event is rejected. These controls protect
against accidental corruption and process races. Same-UID tampering is not a
trusted security boundary. The module does not itself authenticate the caller
who asserts `server_reconcile` or `exposure_reconciled`; the integrating owner
worker must validate those facts first. A `True` policy result does not
authorize process launch or replace a claim, reservation, admission record, or
external launch fence.

The TLA model explicitly keeps terminal evidence monotonic for the pinned
invocation and models a foreign invocation as a separate conflict that keeps
exposure held. An early exploratory trace had shown that allowing a generic
unknown-process event after observed exit would regress terminal knowledge; the
source/model now reject that stale same-invocation transition rather than
weakening the stop invariant. TLC/SANY, mutation check and source tests remain
pending the root resource gate. The root owns independent review, publication,
API coordination, and any canary/model plan.

## Remaining execution-controls inventory

Published source includes project-scoped central/local CPU controls with durable generations (`src/skybuild/admission.py` and `src/skybuild/api.py`), claim-fenced CPU reservations and a no-command dispatch simulator (`src/skybuild/cpu_dispatch.py`), a bounded read-only task execution snapshot (`src/skybuild/execution_status.py`), reservation/fence-bound observations (`src/skybuild/observations.py`), and a bounded durable local spool (`src/skybuild/observation_spool.py`). These are preparatory controls. `api.py` remains explicitly launch-free; the CPU dispatch simulator runs no OS command or callback.

The task's large remaining execution-control acceptance is not a safe one-shot implementation. It still needs, among other things:

- a durable local launch journal, process identity, replay reconciliation, and per-box stop acknowledgment;
- a central cached usage endpoint/collector with a qualified provider/account/pool, freshness, cost, and billing boundary;
- task and approval-window accounting, aggregate daily/interval caps, reservations and uncertain exposure, plus drain/cutoff/closeout enforcement;
- isolated credential profiles, verified subscription-only continuation, and fenced account/engine failover;
- cached offline finish-current-task/checkpoint-stop policy with reconnect reconciliation;
- independently observed CPU result reporting and task-linked resume artifacts/owner questions.

**Candidate bounded source slice:** add a read-only execution-status panel to the existing task detail page using the current `GET /tasks/{task_id}/execution-status` response. Render the claim, reservations, effects, and observations with their recorded state and explicit unknown/truncation labels; show no aggregate “safe/completed/stopped” verdict. Keep the panel inert: no probes, mutations, releases, dispatch, or model calls. Test exact task scoping, missing observations as unknown, truncation, stale claim/reservation overlap, and DOM text handling. Candidate files: `src/skybuild/static/workbench.html`, `src/skybuild/static/workbench.js`, a small CSS addition if needed, and focused `tests/test_web.py` fixtures. This is a useful section-9 console increment independent of a model provider profile.

**Profile gate:** no frontier or shared model profile can be selected from this source review. The owner-provided suspension bars Brodson use; no subscription account, provider usage source, enforceable billing mode, or endpoint-polling cost was qualified here. Keep inference parked until an owner-selected profile passes those qualification checks. Do not treat the CPU-only panel or simulator as a model-launch MVP.
