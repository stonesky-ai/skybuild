# SkyBuild audit and coordination handoff A2

Written for Kevin's replacement Codex session. Read this alongside the other session's handoff, expected at `/tmp/hand-a1.md` (verify the filename; it was being written). This document records the audit session, not a second implementation stream.

## Owner request and latest direction

Kevin asked for an examination of overlapping sessions, tasks, and branches around the REST migration and the remaining path to an automatically deployed worker that pulls work from REST, executes it, returns a valid result, and gets the result reviewed and merged.

Kevin subsequently authorized communication and cooperation between local Codex sessions. He instructed that this session's case/follow-ons wait behind Petri in landing to dev. He is closing this session after this handoff and will start a replacement with both handoffs to resolve problems.

No new credentials are needed for communication between sessions on this box. Use the shared coordination directory described below. References below to worker permissions concern the existing REST execution contract, not access between local Codex sessions. Do not make Kevin coordinate duplicate implementation streams.

## This session's identity and changes

- Audit session: `01a12360-6c7f-78a1-a288-c62de675b24d`.
- Working root: `/home/kevin/my_code`.
- Primary repository inspected: `/home/kevin/my_code/skybuild`, origin `stonesky-ai/skybuild`.
- I own no code branch, PR, gate, worker, or publication operation.
- I made no repository source edits, Git mutations, task-state mutations, credential changes, service starts, model launches, or merges.
- I wrote audit snapshots in `/tmp`, a response in the shared coordination directory, and four owner-authorized Cord coordination notices. No background process or subagent was started by this session.
- Test counts below are evidence from other sessions and retained artifacts, not tests run by this audit session.

## Latest verified repository state

This supersedes the earlier audit messages that said Petri was still awaiting its gate.

At approximately 2026-10-10 02:01 UTC / 2026-10-09 21:01 CDT:

- Petri PR #68 is **merged**: https://github.com/stonesky-ai/skybuild/pull/68
- Final reviewed Petri PR head: `4719d8129eff6bc70d2b52a6d70312718b16f1a5`.
- Published dev-003 commit: `6bb9d6e2e816bdb3721dd791daff376fd1b46ec9`.
- Full disposable PostgreSQL gate: **1,838 passed, 1 skipped, 1 warning in 215.74 seconds**, cleanup confirmed.
- Tested and published tree: `3e2a4f54bb895104773e8f85fc4d07d88b1d8290`.
- All 12 frozen Petri task heads and the five committed design/workflow documents are included. Earlier failed candidates were corrected and reviewed before the successful run.
- Dev-003 to main PR #49 is also **merged**: https://github.com/stonesky-ai/skybuild/pull/49
- Remote main: `50e6dfb4c622114f97b348bf679e8a45191aa2a4`, merged at 2026-10-10 01:54:42 UTC.
- Remote dev-004: `06b5808ea5a1bcb39b19b57cf0acf934d48a4c77`.
- Standing next-cycle PR #69: https://github.com/stonesky-ai/skybuild/pull/69, `dev-004` into `main`.
- Pilot briefs PR #67 remains **open**, still targeting **dev-003**: https://github.com/stonesky-ai/skybuild/pull/67
- The main local checkout still reports branch `dev-003` and is clean. Do not start work from an assumed active dev branch without checking remote state and the replacement session's checkout ownership.

The Petri session's latest handoff says code/docs are pushed and merged into main, dev-004/PR69 are ready, and all its subagents have completed. Do not rerun its implementation or its successful gate merely because earlier messages described them as pending.

Retained Petri evidence lives under `/home/kevin/my_code/skybuild-gate-tmp/petri-implementation/`, including `gate-pr-petri-003.json`, `review-prepared-003.json`, and `integration-proof.json`. Reconcile exact artifact contents before relying on them for a later changed candidate.

## Latest live task state

I made an authenticated, read-only REST observation at `2026-10-10T02:01:19.865446+00:00`.

- `/health/ready` passed earlier in this session; task reads continue to succeed.
- 69 tasks: **11 done, 39 blocked, 16 deferred, 1 in-progress, 2 proposed; zero Ready**.
- `SKYBUILD-ARCHITECTURE` and `SKYBUILD-BOOTSTRAP` are now Done, accepted by the pilot session during this audit.
- `SKYBUILD-TASK-CUTOVER`, `SKYBUILD-MANUAL-WORKER-PILOT`, `SKYBUILD-PETRI`, and the staged coding tasks still show blocked/reassess.
- `SKYBUILD-PETRI-12` explicitly says to review merged source for task acceptance; runtime promotion needs separate authorization.
- CPU controls read `pool: null`, `held_units: 0`.

These task statuses do not mean that all corresponding code is missing. Dependency edits invalidate assessments, and source integration is separate from task acceptance. Use current API records and retained acceptance evidence to reconcile them; do not repeat successful migrations, imports, or implementations to make the status look current.

The last independently retained deployment evidence identifies schema 12 and the launch-free API. I did not inspect a fresh database migration list at handoff time. Do not infer migration 013 deployment from the Git merges. Recheck the accepted runtime before using Petri-aware workers against it.

## Existing sessions and ownership

1. **Pilot and integration session** `01a11f78-608a-7403-82b5-67bceb858de9` owns PR67 and the staged manual REST worker pilot: collector, freshness, importer fidelity, local controller/runbook documentation, and Wonko validation. Its handoff should be A1. It held PR67 while Petri landed. It must now reconcile the new dev-004 cycle and Petri contracts before resuming.
2. **Petri session** `01a122f0-634d-7fa2-ab4c-81996d0995be` owned the 12-task workflow stack and PR68. Its source integration and dev closeout are now complete. Initial combined task branch was `task/petri-12-acceptance` at `4f2ce28f7994c14d001ce57d66b8da08c771ebac`; later reviewed bundle corrections produced final PR head `4719d81...`. Do not mistake the old head for the final published candidate.
3. **Runner/diagnostics and cleanup session** `01a12276-9f56-7221-a7bc-8e2a89fe6168` completed runner and Marshall diagnostics work through PR66. Consult its ownership before removing its worktrees.
4. **Backup/legacy groundwork session** `01a12231-a564-7c72-8cbb-bff254920007` left uncommitted backup work and a legacy migration census. It reported those as ready for review, not complete or pushed.
5. **This A2 session** performed only audit and coordination. There is no A2 implementation branch to merge after Petri.

## Coordination already recorded

Shared local directory: `/home/kevin/my_code/skybuild-session-coordination/`.

Read:

- `landing-coordination.md`, written by the pilot session. Some branch/hold details predate Petri's successful closeout.
- `reply-01a12360-6c7f-78a1-a288-c62de675b24d.md`, my audit response. Its statement that PR68's gate is starting is also superseded by the latest repository state above.
- `delivery-status.json` and any newer reply files.

Direct `codex queue` attempts failed before delivery because this sandbox cannot initialize the read-only Codex state databases. The existing `/home/kevin/.codex/ipc/ipc.sock` refused connections. The pilot session independently found the same limitation. Do not spend more time on credentials or invent a daemon/recovery flow to coordinate this handoff; shared local files already work. Shared files do not automatically wake another session.

I also sent durable Cord notices, authenticated as the existing owner principal and clearly attributed to this audit thread, to `pilot_owner` and `pilot_dispatcher`. Initial message IDs: `7f3ef623-f934-475b-8a4c-6a15ccb5ea9f` and `7492c305-e95c-4285-9c7d-d6c2ead19600`. Compatibility finding IDs: `f898670f-39e4-498e-b218-e653bc44fec3` and `8b180973-cd78-48a2-a9c2-0ea36eba508f`. They grant no launch or execution authority. No receipt or acknowledgment was confirmed during this audit. Do not resend them as new assignments.

## Findings to reconcile with A1 before coding

### 1. Pilot freshness brief conflicts with Petri's revision changes

At the reviewed Petri source inspected by this audit, `manual_cord._claim_assignment` calls `claim_task`, advancing the task revision. `_submit_result` then submits author output into Validating, advancing it again. `manual_result.py` was unchanged by the initial Petri stack.

The staged `SKYBUILD-PILOT-RESULT-FRESHNESS` brief at `d425530cf07a8e11006f088740c7346eabdd5069` requires current task revision to equal the original manual-work-v2 assignment revision and requires matching ready/in-progress status. Implementing that literally for Petri-enrolled tasks would reject legitimate claimed/submitted results.

Recheck the final merged Petri code because bundle corrections occurred after my initial inspection. Amend the pilot brief and acceptance checks before dispatch. Bind current attempt, fence, input generation, exact output/head/base, and policy through the accepted workflow contract. Preserve stale-result denial and legacy relay compatibility. The pilot session owns this work; do not start a duplicate freshness implementation.

### 2. Result collector still expects the old dispatcher permission set

Live dispatcher and both local workers/Wonko were verified with exactly `cord:handle`, `cord:read`, `cord:send`, and `tasks:read`. Access-only Cord round trips passed.

The inspected collector `receive_result` accepts exactly three Cord grants and therefore rejects the current dispatcher. `SKYBUILD-PILOT-RESULT-COLLECTOR` is a genuine bounded first coding task, not redundant administration.

The new Petri-aware worker profile separately supports `--workflow` and requires task claim/write permissions. Existing four-grant profiles remain suitable for the old manual relay. Qualify the selected profile at runtime adoption; do not silently broaden it or confuse these API permissions with local session communication.

### 3. Import workflow fidelity defect

Private diagnostic: `/home/kevin/my_code/skybuild-pilot-state/cutover-receipt-diagnostic-20261010.json`.

The reviewed v2 plan and authority receipt match the 38-task import. Import SQL omits assignee/blocker from insert and replay comparison. The original architecture assignee became NULL. This is a field-projection defect, not evidence that the authority switch failed.

`SKYBUILD-IMPORT-WORKFLOW-FIDELITY` is the pilot-owned stage-two task. Fix future import/replay behavior and test it with disposable databases. Do not rerun the successful live import. Any live data correction needs an explicit, history-preserving corrective workflow.

### 4. Stale or overlapping follow-ons

- PR66 merged runner fixes, Marshall diagnostics, and pilot/cutover documentation. It passed 1,238 tests, 1 skipped, with cleanup and tested/published-tree equality. Retained evidence: `/tmp/skybuild-pr66-wonko-passed-integration.json` and `/tmp/skybuild-pr66-wonko-passed-run.json`.
- `SKYBUILD-PYTHON-MARSHALL-COMBINED-INTEGRATION` overlaps that completed bundle.
- `SKYBUILD-MANUAL-REST-DEPLOYED-ACCESS-QUALIFICATION` overlaps verified dispatcher/local/Wonko access proofs. Check its full acceptance before closing it, but do not redo access setup blindly.
- Old branches can have non-ancestor tips even when corrected successors delivered their implementation. A Git ancestry mismatch alone is not proof of missing work or permission to merge an old branch.

### 5. PR67 needs a new target and refreshed evidence

PR67 is still open against dev-003, but dev-003 has now closed into main and dev-004 is the active next cycle. Reconcile with A1, select the intended current target, and rebase/refreeze accordingly. Refresh brief source pins, task revisions, review evidence, and required candidate gate. Do not merge the old candidate merely because its docs-only source review passed before Petri.

## Remaining path to the requested automatic worker

The REST task authority is live. Source integration of Petri is now complete. A qualified automatic deployed worker loop is still not established by those facts.

1. Reconcile A1/A2, current refs, task acceptance, and PR67. Preserve one implementation owner and one publisher.
2. Complete the staged pilot: one real local collector code task, then concurrent local freshness/importer tasks, then the remote Wonko stage. These are substantive coding demonstrations; documentation alone is not the parallel proof.
3. Select a small explicit execution contract for deployed helper/shell/Python tasks and approved model profiles. Connect REST selection/claim, renewals, exact pinned worktrees and inputs, execution, durable reports, and interrupted-attempt reconciliation. A second task queue is unnecessary.
4. Finish safe real-process lifecycle and resource admission. Existing CPU dispatcher is a fake simulator. The systemd job-unit helper is an execution primitive; its stop operation was deliberately disabled because a unit-name check cannot safely stop a replacement invocation. Neither helper establishes complete sandboxing or managed execution by itself.
5. Finish worker installation, version pinning, update/restart and selected-host qualification. Connect one allowed model route with enforceable existing subscription/budget/stop controls before unattended model work. All-provider completion, cloud bursting, and unrelated legacy extraction need not block a SkyBuild-only demonstration. Brodson remains suspended.
6. Connect exact result evidence to independent review, frozen bundle testing, publication, confirmed task inclusion, and task acceptance. Existing bundle tools already perform useful parts; the complete automatic runtime chain is not yet demonstrated.
7. Demonstrate two isolated attempts, one rework/interruption recovery, and a compatible controlled controller update while the accepted controller remains usable.

Do not turn the audit into a competing executor implementation without checking A1's current scope and accepted source first. Do not infer worker deployment or live migration authority from repository merge approval.

## Other work to preserve

At the original worktree audit:

- `/home/kevin/my_code/skybuild-daily-db-backup`: modified `scripts/create_pilot_backup.py`; capture-integrity groundwork only. Scheduling, destination, retention/encryption policy and publication remained unresolved.
- `/home/kevin/my_code/skybuild-legacy-migration`: untracked `docs/design/research/legacy_migration_census_20261009.md`.
- `/home/kevin/my_code/skybuild-dev-003`: modified `tests/test_admission.py`.
- `/home/kevin/my_code/skybuild-preserved-work`: numerous dirty files, including uncommitted model-work gate/launcher code. That old model-work implementation reads the retired Markdown ledgers and must not be restored as the current task authority.
- `task/cron-supervisor` is an explicitly disabled prototype for one prepared Codex session. It does not select REST tasks or qualify the automatic worker.

This inventory predates later cleanup/integration. Recheck current state and ownership; do not clean or adopt another session's dirty work.

## Evidence and startup instructions

- Latest live task snapshot: `/tmp/skybuild-audit-20261009-tasks.json`, refreshed at 02:01:19 UTC.
- Earlier branch/worktree snapshot: `/tmp/skybuild-audit-20261009-branches.json`. It originally contained 73 branches and 62 registered worktrees; those counts are historical, not current admission evidence.
- Detailed coordination record: `/tmp/skybuild-coordination-01a12360.json`.
- Pilot access evidence: `/home/kevin/my_code/skybuild-pilot-state/local-access-proof-20261010/`, `local-worker02-access-proof-20261010/`, and `wonko-access-proof-20261010/`.
- Existing API endpoint used for the read-only audit: `https://jeltz.tail991ac1.ts.net:8443`, with the existing pinned CA and protected token-file setup. No secret values appear in this handoff. Do not print tokens or read `.env` files to reconstruct access.

Read current repository `AGENTS.md`, the RTK instructions/available skills, and Caveman full instructions. Use this session's own RTK executable. For indexed code, verify CodeGraph's exact project path before navigation; do not reuse another checkout's index or initialize one contrary to Kevin's instructions. Honor the current review, integration and host-memory gates. The owner-directed full PostgreSQL gate uses a 6 GiB admission floor; it is not permission to erase the separate host/worker reserve.

Start by reading both handoffs and checking remote main/dev-004, PR67/PR69, live task state, and any still-active worktrees. The major implementation already landed: resolve the pilot integration boundaries and stale task evidence before generating more overlapping work.
