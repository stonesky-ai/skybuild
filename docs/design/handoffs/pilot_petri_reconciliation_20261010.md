# REST pilot and Petri reconciliation

Owner direction: reconcile both handoffs into dev-004, then demonstrate one README task through the workflow and merge to dev before extending the MVP pilot. This document records source and runtime boundaries; it does not attest task completion or deploy services.

## Retained handoffs and source

- [A1: REST pilot](rest_pilot_a1_20261010.md), imported from `/tmp/hand-a1.md`. The requested underscore filename did not exist.
- [A2: audit](rest_audit_a2_20261010.md), imported from `/tmp/hand_a2.md`. A2 owns no implementation branch.
- Petri PR 68 is merged at `6bb9d6e2e816bdb3721dd791daff376fd1b46ec9`; dev-003 PR 49 reached main at `50e6dfb4c622114f97b348bf679e8a45191aa2a4`.
- Current dev-004 source base is `06b5808ea5a1bcb39b19b57cf0acf934d48a4c77`, with standing dev-to-main PR 69. Both Petri and runner/Marshall PR 66 are ancestors.
- The unfinished PR 67 targeted closed dev-003. Its six-brief source `d425530cf07a8e11006f088740c7346eabdd5069` is retained in Git. A fresh bundle must target dev-004 and supersede the old candidate deliberately.

Petri's final default gate passed 1,838 tests, with one skip and one warning. Its tested/published tree is `3e2a4f54bb895104773e8f85fc4d07d88b1d8290`. Retained exact receipts are under `skybuild-gate-tmp/petri-implementation/`: `gate-pr-petri-003.json`, `review-prepared-003.json` and `integration-proof.json`. These certify that source only; they cannot certify the changed reconciliation candidate.

## Refreshed assignment contract

All six briefs retain stable task IDs, workers, branch names and owned paths. New assignment IDs end in `-002`; old messages or durable intents must never be reused with changed brief bytes. The actual committed brief and base are hashed at dispatch. Each check fits the receiver's 300-character limit, and each model limit fits its 200-character limit. Original briefs exceeded these limits.

The collector task remains a bounded fix for the expected non-admin dispatcher: exactly `tasks:read`, `cord:read`, `cord:send`, `cord:handle` in project skybuild. Petri worker credentials are a different profile, adding `tasks:claim` and `tasks:write`, with explicit `--workflow` selection. Existing credentials are not silently broadened.

Freshness must follow the selected contract. Legacy API-bound results retain exact revision and eligible-status matching. Petri claim and submission advance revisions; successful submission moves Working to Validating. Bind current project/task, attempt, fence, input generation, definition and policy versions, exact submitted head/branch/base, and durable assignment/claim/submission evidence. Missing binding or unavailable workflow reads block automatic review while retaining verified result/Git evidence. Never reconstruct historical ownership from the current task alone. No new classifier, queue or automatic model launch is required.

The importer task fixes future insert/replay fidelity for assignee and blocker. The successful 38-task import must not be repeated. A live correction must preserve append-only history and its own explicit scope.

Use each checkout's `scripts/project_python` for commands and verify the source import path. Historical capacity numbers do not qualify current host admission. Later operator/runbook work must preserve the Petri section, fenced claims, durable intents, CPU renewal and separate access, coding and publication evidence.

## Runtime observation and acceptance

Authenticated read-only observation at `2026-10-10T02:11:20.509926+00:00` found a healthy readiness endpoint and 69 tasks: 11 done, 39 blocked, 16 deferred, one in progress and two proposed; none Ready. The Petri workflow route for `SKYBUILD-PETRI-12` returned HTTP 404. This is evidence that the selected endpoint does not expose that route, not a fresh SQL migration audit. Do not infer migration 013 or workflow enrollment from Git ancestry. Existing worker proofs cover access only.

The source acceptance and runtime promotion boundaries remain separate. Refresh task definitions with guarded expected revisions and idempotency keys. Dependency edits invalidate affected assessments. Do not attest completed code as missing because its task is blocked; also do not mark the importer/cutover or manual-worker pilot complete despite unmet acceptance. Petri source records contain older review/gate fields as well as newer merge evidence; retain historical attempts and append current receipts rather than erasing failed attempts.

## Next execution sequence

1. Finish exact-head review, frozen preparation and the default combined gate; publish only the passed reconciliation candidate to dev-004. Keep standing PR 69 open.
2. Prepare the README smoke task using one randomly selected README and an execution-time `modified on` line with timezone. Verify or qualify the accepted Petri deployment and worker profile before live transitions. Exercise actual tools and retain observed state changes, checks, independent review, bundle testing and confirmed dev inclusion. Do not substitute fabricated transitions or a legacy relay for Petri proof.
3. Resume one real collector code assignment, then concurrent disjoint freshness/importer coding, then Wonko's remote validation. The README smoke test and documentation alone do not satisfy these substantive stages.
4. Build the bounded REST worker execution/update path next, keeping automatic lifecycle, resource admission, inference billing protection and runtime promotion under their own controls. Brodson remains suspended. The fake CPU dispatcher and systemd primitive do not establish the complete executor.

The old mastertodo/deferred/alreadydone files are retirement notices. REST already owns their imported tasks. Reconcile current IDs, acceptance and backlog; do not create a second queue or repeat the cutover.
