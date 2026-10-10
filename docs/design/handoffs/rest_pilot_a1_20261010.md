# SkyBuild handoff A1: REST pilot session

Read this together with the other session's handoff before making changes. This document covers the long-running REST cutover/manual-worker session, not the Petri implementation session. Reconcile current GitHub refs, live API records, and both handoffs; older task states and branch names below are historical evidence, not instructions to restore them.

The owner requested this handoff so a new terminal can consolidate the two sessions. No merge, deployment, migration, or worker launch was performed while writing it.

## Current verified landing state

The owner said Petri has landed in main and the next dev cycle is open. GitHub and Git ancestry checks confirm:

| Item | Verified state |
| --- | --- |
| PR 66, runner/Marshall/pilot documentation | Merged into dev-003 at bcc55621d1990c5bcc5694e7b86258a6039467b0 |
| PR 68, Petri workflow bundle | Merged into dev-003 at 6bb9d6e2e816bdb3721dd791daff376fd1b46ec9 |
| PR 49, dev-003 to main | Merged at 50e6dfb4c622114f97b348bf679e8a45191aa2a4, 2026-10-10 01:54:42 UTC |
| Current published main | 50e6dfb4c622114f97b348bf679e8a45191aa2a4 |
| Current published dev-004 | 06b5808ea5a1bcb39b19b57cf0acf934d48a4c77; commit subject: docs: start dev-004 cycle |
| Standing next-cycle PR | https://github.com/stonesky-ai/skybuild/pull/69, dev-004 to main |
| Our unfinished PR 67 | OPEN, unmerged, still targets dev-003; https://github.com/stonesky-ai/skybuild/pull/67 |

Petri's dev merge is an ancestor of main, and main is an ancestor of dev-004. These checks passed. PR 66 is also included through that history.

The shared checkout /home/kevin/my_code/skybuild is clean but still on dev-003 at 6bb9d6e2e816bdb3721dd791daff376fd1b46ec9. Another session advanced it; do not reset it to this session's former head.

The existing worktree /home/kevin/my_code/skybuild-p5-petrinet now holds dev-004 at 06b5808ea5a1bcb39b19b57cf0acf934d48a4c77. It belongs to the other session. Verify ownership and cleanliness before using it. Do not force-checkout a branch already attached to another worktree.

## Owner direction and constraints

- Continue toward a useful self-building MVP. Task API cutover and actual workers are the goal, not more planning-only artifacts.
- Brodson is disabled for ALL use until further notice.
- The owner selected a staged manual pilot: one genuine local coding task; then two concurrent local coding tasks with disjoint owned paths; then one substantive remote Wonko assignment. Documentation tasks do not count as the two-worker coding proof.
- Existing helpers/scripts should run without model involvement where possible. The owner asked about shell/Python/helper execution, isolation, and provider-specific small/medium/large model profiles. We reported these as partially scaffolded, not a completed unified executor.
- The owner explicitly authorized cross-session communication and cooperation.
- The owner told this session to HOLD its dev merge while new code landed. That code has now landed, but PR 67 was not rebased or resumed. Do not merge its stale dev-003 candidate. Reconcile both handoffs and prepare the next work against dev-004.
- Do not touch SkyKeep. Do not kill other sessions, clean their worktrees, drop caches, or restart shared services.
- Prefer existing subscription allowances; no paid API fallback or unqualified remote model launch.
- Use RTK. Read personal/repository instructions and applicable skills. Use the current checkout's CodeGraph index only; do not create an index without the owner's direction.
- Every code task needs independent exact-head review. Integrate ready tasks in frozen bundles, up to 20 tasks, using guarded preparation and the default combined gate. Keep PR 69 as the standing dev-004-to-main PR.
- The disposable PostgreSQL gate's authorized admission floor is 6 GiB. Local memory has often been below it. Wonko ran the accepted full gate. Lightweight local work was owner-authorized; do not weaken the heavy gate.

## Completed work from this session

### Runner and Marshall bundle: PR 66

Reviewed head: 75d4a5a089b28f27cdd8b2b93df26278904bac8d.
Base: e8c58d7109382c0efdf68281326f412aa6df46b6.
Tested candidate: 233ec7c0e22e5586f9d464233b194a2b02d988ef.
Tested/published tree: 26e908a979a1f9067c1b03a67e82818150ac9be4.
Published dev merge: bcc55621d1990c5bcc5694e7b86258a6039467b0.

The checkout-safe project Python runner clears inherited UV_WORKING_DIR, pins trusted src/scripts/root import paths, preserves safe-path protection, and fixes nested gate and Marshall imports. Marshall exposes only three exact static admission errors without leaking arbitrary stderr. Documentation reconciles API authority, staged pilot scope, and Brodson suspension.

Validation: 88 focused checks; independent exact-head source and documentation reviews; required default full disposable PostgreSQL gate on Wonko: **1238 passed, 1 skipped, 1 warning in 270.76 seconds**. Cleanup, source ancestry, and tested/published-tree equality are confirmed. Earlier failed gate artifacts remain historical failures; do not mistake them for the final accepted run.

Retained accepted evidence:

- /tmp/skybuild-pr66-wonko-passed-integration.json
- /tmp/skybuild-pr66-wonko-passed-run.json
- /tmp/skybuild-pr66-runner-fix-exact-review.txt
- /tmp/skybuild-pr66-final-review.txt
- PR 66 body was updated with the final passing result.

The live API now lists SKYBUILD-PROJECT-PYTHON-RUNNER and SKYBUILD-MARSHALL-ADMISSION-DIAGNOSTICS as done, revision 4 each. Do not implement their old branches again.

### Architecture and Bootstrap acceptance

This session used the existing owner/admin completion endpoint, exact current revisions/generations, and real retained checks/review/publication evidence to record:

- SKYBUILD-ARCHITECTURE: done, revision 2.
- SKYBUILD-BOOTSTRAP: done, revision 4.

Evidence:

- /home/kevin/my_code/skybuild-pilot-state/skybuild-architecture-attestation-20261010.json
- /home/kevin/my_code/skybuild-pilot-state/skybuild-bootstrap-attestation-20261010.json
- Original accepted bootstrap source: 1869904ee442dd626b185143ac730ecc4538b9dc.
- That commit's docs/design/implementation/bootstrap_review.md records independent **114 passed, 1 warning**. All 24 final manifest blobs were checked against that Git commit before attestation.

Completion changes invalidate dependent readiness and revisions. Blocked downstream records do not mean Bootstrap code must be rebuilt.

### Access-only local and remote worker qualification

pilot_dispatcher, local_worker_01, local_worker_02, and wonko have project skybuild's exact relay grants: tasks:read, cord:read, cord:send, cord:handle. Distinct scoped credentials, private TLS, and access-only Cord receipt/reply/handling passed.

Evidence directories, each containing result.json:

- /home/kevin/my_code/skybuild-pilot-state/local-access-proof-20261010/
- /home/kevin/my_code/skybuild-pilot-state/local-worker02-access-proof-20261010/
- /home/kevin/my_code/skybuild-pilot-state/wonko-access-proof-20261010/

These explicitly say coding_assignment=false and model_launched=false. They are NOT evidence that the real coding pilot completed.

## Unfinished PR 67 and prepared pilot tasks

PR 67 contains six JSON assignment briefs only. No implementation of those six assignments was done through this pilot.

Source branch: task/staged-worker-briefs-20261010.
Source head: d425530cf07a8e11006f088740c7346eabdd5069.
Source worktree: /home/kevin/my_code/skybuild-staged-worker-briefs-20261010.

Old frozen bundle branch: bundle/staged-worker-briefs-001.
Old PR head: f18c52cbc3924582dbe3fb4932102647402b1e0e.
Old base: bcc55621d1990c5bcc5694e7b86258a6039467b0.
Old tree: ff0827c7281a23fbab5c990f6fdc0e59e7e1f1c6.
The exact six brief blobs matched the independently reviewed source branch.

Evidence:

- /tmp/skybuild-staged-worker-briefs-review.txt
- /tmp/skybuild-pr67-exact-review.txt
- /tmp/skybuild-staged-briefs-wonko-manifest.json
- /tmp/skybuild-staged-briefs-wonko-report.json
- Remote retained candidate: /tmp/skybuild-staged-briefs-wonko-Vqwa2s7z/prepared/candidate on Wonko.
- /tmp/skybuild-staged-worker-task-payloads.json is historical one-time provisioning input, NOT a queue or current API snapshot. Do not blindly rerun its old creation scripts.

The full PR 67 gate never started. Its source review is for the old base and cannot certify post-Petri compatibility or current dev integration.

Live task records exist:

| Task | Intended assignment | Last observed status/revision |
| --- | --- | --- |
| SKYBUILD-PILOT-RESULT-COLLECTOR | local_worker_01; fix collector's exact three-grant check to match qualified four-grant relay | blocked/reassess, r3 |
| SKYBUILD-PILOT-RESULT-FRESHNESS | local_worker_01; validate result freshness against actual task/attempt state | blocked/reassess, r3 |
| SKYBUILD-IMPORT-WORKFLOW-FIDELITY | local_worker_02; preserve assignee/blocker in future imports and replay comparisons | blocked/reassess, r3 |
| SKYBUILD-PILOT-LOCAL-CONTROLLER | local_worker_02; operator documentation follow-up | blocked/reassess, r4 |
| SKYBUILD-PILOT-STAGED-RUNBOOK | local_worker_01; manual pilot runbook follow-up | blocked/reassess, r4 |
| SKYBUILD-PILOT-WONKO-REPORT | wonko; actual remote relay/worktree/check/report proof | blocked/reassess, r4 |

Freshness and importer work depend on accepted stage-one collector completion. The two code tasks own disjoint files: manual_result.py/test_manual_result.py versus importer.py/test_importer.py. Documentation follows the substantive code stage. Do not dispatch two assignments to the same worker simultaneously.

SKYBUILD-MANUAL-WORKER-PILOT remains blocked/reassess, r5. No real coding assignment has completed through this pilot. Preserve its genuine coding-stage acceptance.

## Important new finding from the other audit session

Read /home/kevin/my_code/skybuild-session-coordination/reply-01a12360-6c7f-78a1-a288-c62de675b24d.md before updating or dispatching the briefs.

The audit reports that the result-freshness brief's original strict task-revision/status comparison is incompatible with Petri workflow adoption: manual_cord claim and result submission advance the task revision, and submission moves work to Validating. Implementing the old brief literally could reject valid results.

Verify this against current dev-004 and the other handoff. Amend the brief AND API acceptance inputs before dispatch. The audit recommends binding actual attempt identity, claim fence, input generation, exact output/head/base, and policy through the accepted workflow contract, while preserving legacy relay compatibility and stale-result denial.

The audit also reports Petri's explicit workflow worker mode requires tasks:claim and tasks:write in addition to relay grants. Current pilot identities have only the four relay grants. Do not silently broaden credentials or assume source integration deployed the new workflow.

Petri source merging does not prove migration 013, workflow enrollment, new worker credentials, or runtime promotion occurred. This session verified the earlier schema-12 deployment/access path; it did not deploy Petri. Let the other handoff establish any newer runtime evidence.

## Cutover import defect: do not repeat the live migration

The API owns the tasks; the three Markdown ledgers are retirement notices. Preserve that authority.

The retained v2 receipt and reviewed plan match for 38 imported tasks, but original SQL omitted assignee/blocker. Exactly one original planned non-null field was lost: SKYBUILD-ARCHITECTURE's assignee text became NULL. The original journal hash therefore does not exactly match the full reviewed plan; its normalized SQL projection matches.

Evidence: /home/kevin/my_code/skybuild-pilot-state/cutover-receipt-diagnostic-20261010.json.
Retained original plan: /tmp/skybuild-current-ledger-import-plan.json.

SKYBUILD-TASK-CUTOVER remains blocked/reassess, r7. Do not attest unchanged lossless-preservation acceptance as satisfied. The importer task fixes future imports/replay checks; any live correction must preserve append-only history and be separately explicit. Do not reimport, rerun cutover, rewrite old journal events, or restart services to clear this task.

## What is still not operational

The manual receive/validate/prepare-worktree/result tools exist. The automatic CPU dispatch adapter is fake-only. scripts/skybuild_job_unit.py is a bounded systemd execution primitive with memory/runtime/environment controls, not a connected REST executor or a qualified security sandbox. Its physical stop is intentionally disabled pending a safe identity-bound effect.

Automatic worker install/update, real typed helper/shell/Python execution, qualified provider/model profile selection, enforceable inference controls, and the full automatic result/review/publication loop remain unfinished according to this session and the audit reply. An uncommitted legacy model launcher in another preserved worktree reportedly reads retired ledgers; do not restore it as the current executor.

## Current API snapshot and stale task records

A fresh authenticated read during handoff creation returned 69 tasks. Architecture, Bootstrap, runner, and Marshall diagnostics are done. Petri-12 is still blocked/reassess, r153, with next action to review merged source for task acceptance; this does not contradict its source landing.

SKYBUILD-PYTHON-MARSHALL-COMBINED-INTEGRATION remains proposed, r1, despite PR 66 completing that work. The audit also identifies SKYBUILD-MANUAL-REST-DEPLOYED-ACCESS-QUALIFICATION as overlapping completed access proof. Reconcile from retained evidence instead of duplicating implementation. Refresh API definitions, revisions, current completion, claims, effects and ownership before every transition or dispatch.

## Cross-session communication and worktree ownership

Sender session: 01a11f78-608a-7403-82b5-67bceb858de9, named Continue previous task.
Petri session: 01a122f0-634d-7fa2-ab4c-81996d0995be.
Audit session: 01a12360-6c7f-78a1-a288-c62de675b24d.
Cleanup session identified earlier: 01a12276-9f56-7221-a7bc-8e2a89fe6168.

Direct codex queue delivery failed because this sandbox cannot initialize writable databases under read-only ~/.codex. The existing ipc.sock refused connection. Do not assume instant inter-session delivery.

Shared notes: /home/kevin/my_code/skybuild-session-coordination/landing-coordination.md and the audit reply above. The audit session owns no implementation branch, gate, or publisher; its reply requests acknowledgment. It also posted owner/dispatcher Cord notices, IDs listed in its reply. This session read that reply while creating this handoff; no Cord acknowledgment was sent.

Last worktree inventory: 63 local worktrees, so guarded preparation lacks its required two-slot headroom. Clean only verified owned, clean, pushed, integrated worktrees. This session released /home/kevin/my_code/skybuild-gate-tmp/project-python-runner-uv-working-dir-fix at 75d4a5a089b28f27cdd8b2b93df26278904bac8d for such cleanup, retaining branches/evidence. It still existed at handoff time. No cleanup occurred here. Preserve dirty backup/legacy/preserved worktrees reported by the audit.

Our subagents finished or are held; no PR 67 gate, model worker, or new polling loop is running from this session. Agent task names from the old terminal are not portable resume commands.

## Access and safe operational references

Private endpoint: https://jeltz.tail991ac1.ts.net:8443.
Controller state: /home/kevin/my_code/skybuild-pilot-state.
CA: tls/ca.crt under that state directory.
Credential files: secrets/pilot_owner-token, secrets/pilot_dispatcher-token, secrets/local_worker_01-token, secrets/local_worker_02-token. Use files; do not print or copy token contents into prompts/logs/handoffs.

Wonko has existing scoped pilot credentials under /home/kevin/my_code/skybuild-pilot-access/ (wonko-token and ca.crt). Its original checkout was kept unchanged on dev-002; use an owned isolated checkout and verify current refs.

Approved SSH workaround for this host's bad system config-file permissions:
ssh -F /dev/null -o BatchMode=yes -o ConnectTimeout=8 -o ConnectionAttempts=1 wonko
Use scp -F /dev/null for copies. Do not chmod system SSH configuration.

Use repository skills and guarded scripts: scripts/prepare_bundle.py, scripts/integrate_reviewed_pr.py, scripts/pr_text.py, and scripts/project_python. Check current CLI help and repository identity before writes. Full default gates ran on Wonko without launching remote Codex; the local subscription model drove SSH commands. Do not infer remote model billing qualification from that.

## Recommended continuation after reading both handoffs

1. Establish one owner for the next publication and confirm the other sessions are finished or explicitly relinquishing scope. Refresh GitHub main/dev-004/PR 69 and the live runtime/task state.
2. Reconcile Petri source acceptance versus deployment/migration/enrollment evidence from the other handoff. Choose the qualified manual or workflow pilot path explicitly.
3. Correct the six brief inputs for current Petri semantics and update affected API task definitions through guarded workflows. Preserve real coding acceptance and clear ownership.
4. Move the unfinished brief bundle onto dev-004 using the normal guarded process. Retarget or supersede old PR 67 deliberately; preserve its evidence. Obtain current exact-head independent review and the required combined gate before merging.
5. Ready and dispatch one current, pinned, unowned collector task through the REST/Cord path. Receive, validate, prepare an owned worktree, run focused checks, commit/push, return result, independently review, bundle and confirm inclusion.
6. Only after accepted stage one, run the two substantive disjoint local coding tasks concurrently, then Wonko's remote proof. Report measured execution and actual artifacts; access proofs alone do not satisfy these stages.
7. Reconcile redundant completed task records and the known import omission without redoing completed code or live migration. Build the simplest real CPU/script worker path next, with model work separate and no model calls for routine waits.

Refresh every SHA, revision, readiness state and evidence binding before acting. This handoff deliberately preserves uncertainties rather than claiming the workers or Petri deployment are finished.
