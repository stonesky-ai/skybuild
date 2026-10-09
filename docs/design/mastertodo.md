# SkyBuild current tasks

Authority: Git-backed planning ledger, revision A34, 2026-10-08. API cutover has not occurred. See [architecture section 4](architecture.md#4-temporary-task-authority-and-transition) for the lifecycle. Order below is proposed priority, not execution authorization. Implementation remains unauthorized.

Each task ID lives in exactly one ledger. Related ledgers: [deferred](deferred.md), [alreadydone](alreadydone.md). These are new SkyBuild project records, not updates to the old SkyKeep queue. Until claims/fencing exist, coordinate any later authorized execution manually and serially.

## SKYBUILD-ARCHITECTURE — Agree the initial architecture boundary

- Status: in-progress (planning only).
- Area: architecture. Dependencies: none. Assignee: owner and current planning session.
- Brief: review the all-tooling extraction boundary, document governance, Markdown/API authority transition and launch-free task/Cord bootstrap. Initial API/PostgreSQL hosting on the laptop and Tailscale website/API reachability from the intended enrolled boxes are confirmed; alternative VPN configuration is deferred. The website outage option defaults to finish-current-task. Owner/admin credentials plus separate project-scoped worker tokens are confirmed. Daily database dump-file commits are planned; restore/journal rebuild testing is low priority, with journal location/interval open. GitLab/Bitbucket support remains deferred; GitHub pushes are the initial project assumption. Task-owned reset/clean, post-merge cleanup and automatic bundled integration after required reviews/combined gates are accepted; protect other workers and shared branches. Task is the canonical work term, with new `/tasks` routes and explicit legacy mappings. Resolve offline limits, concrete credential/scope mechanics and data-contract choices; keep later execution policies explicitly open.
- Acceptance: architecture and ADR statuses agree; implementation implications and task dependencies are current; owner selects the next scope. A planning draft alone does not satisfy owner review.
- Architecture: sections 1–7 and 12. Plan: area 1.

## SKYBUILD-CAPACITY-PLAN — Refine compactness, inference routing and host lifecycle

- Status: in-progress (planning only).
- Area: architecture/capability research. Dependencies: none for read-only planning; execution/rental is not authorized.
- Brief: refine compact logical components and self-build boundaries; assess CPU vs qualified brodson/RunPod/premium work, model-expiry priorities and a configurable Codex/Claude coordinator with automatic qualified failover; define shared endpoint accounting, qualification and accepted-throughput economics; specify start/drain/stop rules for brodson allocations, RunPod, aragog and the initial laptop control host; define mandatory long-task compaction/restart policy and CPU monitoring that avoids repeated large-context prompts; a separate Ubuntu server is a later option only after a tradeoff discussion.
- Acceptance: governing architecture contains capability unknowns, qualification/offload criteria, failure paths and lifecycle/backstop rules; the high-level plan reflects them. Full weekly Claude percentage task budgets, separate short-window checks and the configurable below-1% approval exemption are recorded; time-limited approval and daily caps per frontier provider are confirmed; Claude's default daily cap is 10% of its full weekly allowance; the owner also requires throttling near 90–95% of total provider allowance consumed, including non-build use, to preserve urgent capacity; eight-hour approval, an overall interval cap, central cached usage readings, multiple accounts with rotation near 98%, a combined urgent reserve across comparable accounts, proactive drain before approval limits, expiry compact/commit/handoff with five-minute default configurable closeout grace and a model-query cutoff, task-linked owner questions and subscription-only billing are confirmed; exact taper rules, verified usage sources/polling cost, subscription-only enforcement and remote credential qualification, estimates, interval-cap defaults/account-model-pool mappings, other-provider caps and remaining deadline/provider choices stay open until reviewed. No benchmark or resource start is implied.
- Architecture: sections 8 and 13–17. Plan: capability and rental research.

## SKYBUILD-BOOTSTRAP — PostgreSQL tasks and minimal Cord

- Status: in-progress (owner-authorized implementation, 2026-10-08). Phase: reviewed implementation slice. Responsible: lead. Next action: qualify the restricted runtime database role and actual Tailscale/owner access before any deployment; preserve the launch-free boundary. Local evidence: 114 passing tests against disposable PostgreSQL, real process restart persistence, packaged migrations/assets, and separate Sol review with R1–R3 corrected. See [review evidence](implementation/bootstrap_review.md). This is not full deployed-bootstrap acceptance.
- Area: service. Dependencies: SKYBUILD-ARCHITECTURE (bootstrap boundary agreed).
- Brief: establish hello-world/version/liveness/readiness first, then project-scoped tasks/history and durable mailbox with shared CLI/Python client. Preserve full task briefs, revision conflicts, owner/admin access, separate project-scoped worker tokens and retry deduplication. Provide the initial private Tailscale website/API endpoint for all intended enrolled boxes, with application scopes still enforced and PostgreSQL kept local. No worker launch path.
- Acceptance: implementation-plan area 2 checks pass against disposable dedicated PostgreSQL, including restart persistence and refusal of wrong targets/credentials. A manual client can retrieve a task and exchange a handoff.
- Architecture: sections 3, 5–7. Model requirement when selected: size/capability to be chosen from the actual brief; no vendor fixed.

## SKYBUILD-DAILY-DB-BACKUP — Commit a scheduled PostgreSQL dump daily

- Status: proposed. Priority: normal. Area: database operations. Dependencies: SKYBUILD-BOOTSTRAP provides a usable dedicated SkyBuild PostgreSQL database.
- Brief: schedule a bounded CPU task to dump that database to a file, commit the file/manifest daily and follow the configured GitHub push workflow. Choose format, destination repository/branch/path, run time, retention/size and encryption/key custody before enabling it. Use an isolated worktree, prevent overlapping runs and catch up honestly after laptop-offline periods; no model supervision.
- Acceptance: complete-file/hash and dedicated-target checks pass; partial/failed dumps retain the previous good artifact; daily commit and confirmed versus pending push are recorded; overlap, missed schedule and commit/push failure are handled without unrelated edits or fabricated recovery coverage. Full restore/journal testing belongs to the separate low-priority task.
- Architecture: sections 3–4 and 7. Plan: daily database backups and later recovery. ADR: [0027](../adr/0027-daily-dumps-and-journal-recovery.md). This record does not install or run a schedule during planning.

## SKYBUILD-TASK-CUTOVER — Switch the three ledgers to REST authority

- Status: in-progress (read-only preparation only). Phase: reviewed manifest helper. Responsible: lead. Next action: implement explicit dependency/import mapping and frozen transactional import validation. The Luna helper and read-only CLI preserve raw sections/source hashes and passed separate Sol review; they do not resolve dependency prose or write the API. No live cutover has occurred.
- Area: task authority. Dependencies: SKYBUILD-BOOTSTRAP.
- Brief: define/rehearse a lossless importer, freeze at a commit/hash, validate all three ledgers and history, record one authority switch, then make file ledgers generated/read-only. No bidirectional sync.
- Acceptance: IDs, dependencies, status counts, ordering, full briefs and evidence match; repeat import is safe; conflict import is refused; API/restart/import checks pass; post-write recovery preserves API authority.
- Architecture: sections 4–7. Plan: area 3.

## SKYBUILD-TASK-WORKBENCH — Task page, explicit workflow and immutable journal

- Status: in-progress (thin workbench against disposable tasks). Phase: reviewed basic UI. Responsible: lead. Next action: add remaining structural task operations and guarded workflow/reassessment; live authority still follows cutover. Browser list/create/detail/edit/history, exact dependency IDs, stale-edit refusal, logout cleanup and desktop/mobile layout passed Chromium checks and separate Sol review. Priority: normal. Area: task management. Dependencies: SKYBUILD-BOOTSTRAP; live editable task authority follows SKYBUILD-TASK-CUTOVER.
- Brief: provide outstanding-task status/phase, next action, owner, blockers and journal; edit description/scope/definition of done/considerations, request rework/reassessment, split/merge, set dependencies and defer by date or milestone. Follow the explicit state-machine flowchart and guarded transitions. Bootstrap records manual journaled state; execution controls later enable automatic reassessment and qualified model planning scans.
- Acceptance: every action has a known next state or explicit conflict; journal is append-only with no edit/delete path, including by ADR. Concurrent changes preserve task IDs, lineage, dependency correctness, prior evidence and budget history. Changed inputs invalidate affected readiness; live/unknown effects are reconciled before replacement work. Date/milestone triggers reassess without granting execution authority. Basic controls work with no model available. Multi-person approval chains remain deferred.
- Architecture: sections 5, 9 and 13; [workflow](task_workflow.md). Plan: area 3a. ADR: [0031](../adr/0031-task-workflow-and-append-only-journal.md).

## SKYBUILD-LEGACY-MIGRATION — Extract and port every legacy build API writer

- Status: proposed.
- Area: migration. Dependencies: SKYBUILD-ARCHITECTURE; SKYBUILD-BOOTSTRAP before runtime migration. Read-only census can be selected earlier.
- Brief: census current source/deployed artifacts and all routes/tables/SQL writers/external authorities; separate mechanical API/tests move from PostgreSQL port; rehearse complete import; freeze/fence/verify before each authority switch.
- Acceptance: implementation-plan area 4; every baseline domain accounted for, contention tested, no active SQLite writer or ambiguous route after its cutover, recovery boundary documented. Complete before broad worker resumption.
- Architecture: sections 2 and 7. Historical 27-table v19 list is a baseline, not proof of current schema.

## SKYBUILD-EXECUTION-CONTROLS — Admission, ownership and independent observation

- Status: proposed.
- Area: controlled execution. Dependencies: SKYBUILD-BOOTSTRAP and SKYBUILD-TASK-CUTOVER for SkyBuild tasks; SKYBUILD-TASK-WORKBENCH before automatic adoption; relevant migrated authority for legacy domains.
- Brief: implement durable restrictive controls, generations, fenced claims, atomic reservations/action deduplication, weekly Claude-percentage task budgets, separate short-window checks, configurable approval exemption, eight-hour configurable approval windows with interval caps, proactive pre-limit worker drain and expiry closeout with five-minute default configurable grace/model-query cutoff, aggregate daily caps per frontier provider, central account/model/pool usage collection/REST, isolated multi-account credentials and 98%-used rotation with pooled reserve across comparable accounts under verified subscription-only billing, per-long-task compaction/restart and bounded monitoring context, expiry-aware dispatch, configurable Codex/Claude coordinator with budget-preserving automatic failover and usage/eligibility/circuit rules, run/status/artifact contracts, durable task-linked resume handoffs/owner questions and independent CPU task reporting/observers. Resolve physical launch/stop acknowledgment design before enabling execution.
- Acceptance: implementation-plan area 5 denial/race/partition/observation/budget cases pass; weekly allowance basis and timed approval confirmed, Claude's 10%-of-weekly daily cap confirmed, reserve basis and eight-hour approval confirmed, exact taper rules, usage sources/polling cost, estimates/interval caps/other-provider caps and remaining applicable numeric policy selected before each frontier model canary; website defaults to finish-current-task and caches the selected offline policy; both modes/reconnect are tested; tasks strictly below the configurable 1% threshold need no task approval; uncertain starts retain exposure; no message or watcher bypasses authority; account/engine/host switching, quota exhaustion and inherited credential conflicts cannot enable API charges or purchased usage-credit overage; unknown billing protection parks inference; approaching time/usage limits drains workers without new claims or cap overruns, retaining task-owned WIP commits and discoverable resume summaries; bounded closeout grace ends model queries while CPU tasks report independently; owner questions are task-linked, persistent and cannot renew approval; automatic coordinator failover uses qualified allowed subscription profiles with old-session fencing, retained budgets/deadlines and bounded switch attempts.
- A30 refinement: deliver CPU controls, one qualified zero-charge shared or subscription model profile and additional profiles incrementally. Validate effect-specific authority, allocation subdivisions/settlement, exclusive offline capacity, suspend/window boundaries, trusted credentials and generation-based task reconciliation. No stale result or lost invalidation may leave a false-ready task; every blocked task has a next resolver/action.
- Architecture: sections 5 and 8–9. Plan: area 5.

## SKYBUILD-SHARED-INFERENCE — Use the friend's model pools for qualified build work

- Status: proposed. Priority: normal. Area: inference/project configuration. Dependencies: SKYBUILD-BOOTSTRAP, SKYBUILD-TASK-CUTOVER and the applicable SKYBUILD-EXECUTION-CONTROLS slice; no dependency on all frontier providers or deferred cloud batching.
- Brief: use generic installation-local endpoint and external credential-file settings from `.env`; register separate owner-reported Qwen 4 / Recall 2 / BGE-M3 1 pools; qualify bounded Qwen coding, with independent fact-extraction/retrieval qualification as useful. Add project data scope, allowed work/fallback, pool share and visible quality/latency/rework tradeoffs. Share admission with product tests; protect integration and reduce drafting when validation accumulates.
- Acceptance: implementation-plan area 5b qualification and failure cases pass; aggregate limits hold across projects/workers, zero-charge quota is correctly not applicable, and frontier review/fallback cannot evade subscription caps. Outputs preserve source/profile evidence; useful throughput includes validation, correction and integration. No claim of measured capability from owner-reported slot counts alone.
- Architecture: sections 8 and 13–15. Plan: area 5b. ADR: [0032](../adr/0032-shared-inference-capacity.md). Capability discovery is authorized; runtime implementation, stress testing and infrastructure changes remain unauthorized.

## SKYBUILD-BUNDLED-INTEGRATION — Automatically integrate reviewed task bundles

- Status: proposed. Priority: normal. Area: integration. Dependencies: SKYBUILD-BOOTSTRAP, SKYBUILD-EXECUTION-CONTROLS and relevant migrated integration/gate authority for legacy projects.
- Brief: adapt integration-set/scan concepts into a deterministic fenced pipeline that collects ready reviewed tasks, freezes heads/base/membership, gates a combined candidate and automatically publishes its verified result. Tasks arriving during a long gate collect for the next bundle. Select coalescing/size/age limits and the bundle PR/publication mechanism; core bundling is not deferred cloud-batch optimization.
- Acceptance: one authorized integrator per target; exact tested result and confirmed per-task inclusion; new arrivals do not mutate active candidates; head/base changes invalidate evidence; mixed profiles receive required combined gates; failures/subsets/retries and unknown merge acknowledgment remain bounded and honest. Task acceptance and post-merge cleanup follow verified publication. CPU status exposes backlog/age/gate time without model polling; runtime promotion remains separately controlled.
- A33 refinement: MVP includes existing applicable checks and separate-session adversarial review for every code task, actionable fix/test findings, immutable dispositions and bounded correction. Dedicated complexity/size gates, metric reports, GUI controls and scoped legacy baselines are retained but deferred under SKYBUILD-QUALITY-GATES; they do not block MVP. Stale evidence or self-approval cannot produce acceptance. See [review policy](review_policy.md) and [ADR 0033](../adr/0033-independent-review-and-compactness.md).
- A30 refinement: union required checks; qualify remote head/base enforcement and trusted result origin. Preserve unresolved publication intents across owner changes/timeouts. Journal every member's inclusion, exclusion, failure and final outcome with a known next action. Adapt mergeprep facts/reasons rather than reproducing its old Git/JSON authority.
- Architecture: sections 2, 5, 8–10, 13 and 15. Plan: area 5a. ADRs: [0028](../adr/0028-automatic-bundled-integration.md), [0030](../adr/0030-effect-boundaries-and-accounting.md), [0031](../adr/0031-task-workflow-and-append-only-journal.md). No PR/branch merge is performed during planning.

## SKYBUILD-SELF-BUILD-MVP — Rebuild and extend SkyBuild with parallel workers

- Status: proposed. Priority: high. Area: MVP acceptance.
- Dependencies: useful SKYBUILD-BOOTSTRAP and SKYBUILD-TASK-CUTOVER, minimal SKYBUILD-TASK-WORKBENCH, applicable SKYBUILD-EXECUTION-CONTROLS and SKYBUILD-KEEPER-ADOPTION slices, SKYBUILD-BUNDLED-INTEGRATION and one qualified author/reviewer profile. SKYBUILD-SHARED-INFERENCE applies only when selected; disjoint SkyBuild authority does not require unrelated legacy migration.
- Brief: compose existing components into a running self-building product, rather than stopping at the launch-free API. Keep the accepted controller usable while at least two independent task attempts build features in isolated owned worktrees.
- Acceptance: independently review, combine, test and publish accepted changes; demonstrate a compatible controlled update and one rework/interruption recovery with durable next action and no duplicate work. Shared budgets, slots, model cutoff and stable-controller authority hold. Dedicated complexity/size gates and GUI, all provider variants, complete extraction and cloud/HA features do not block this milestone.
- A34 follow-on: once this milestone runs, prioritize SKYBUILD-QUALITY-GATES and SKYBUILD-QUALITY-DEBT-CLEANUP, then start the serialized SKYBUILD-REPO-REVIEW-CADENCE. These post-MVP tasks do not change this milestone's acceptance.
- Architecture: sections 3, 13 and 16. Plan: MVP delivery target. This is acceptance work, not another subsystem or execution authorization.

## SKYBUILD-KEEPER-ADOPTION — Preserve Keeper and cover every launcher

- Status: proposed.
- Area: worker adoption. Dependencies: SKYBUILD-EXECUTION-CONTROLS and relevant legacy cutover; SKYBUILD-LEGACY-MIGRATION before broad enrollment.
- Brief: adapt existing Keeper/client; reconcile sessions; preserve STOP/DRAIN/checkpoint/update/engines/worktrees; gate or disable every scheduled/manual/nested launch path. Observer precedes separately authorized canaries. Automatic integration adoption depends on SKYBUILD-BUNDLED-INTEGRATION.
- Acceptance: report-only/CPU/model/stop/disconnect/update/reboot evidence for the selected worker; complete launcher coverage; old paths cannot restart work; no jeltz build-worker allocation; owned reset/clean and confirmed post-merge branch/worktree cleanup work without cross-worker/shared-ref destruction or a blanket command ban.
- Architecture: sections 2 and 8–10. Plan: area 6. Starting aragog or a model remains separately authorized.

## SKYBUILD-TOOLING-EXTRACTION — Finish all remaining tooling moves and retire duplicates

- Status: proposed.
- Area: extraction/operations. Dependencies: census from SKYBUILD-LEGACY-MIGRATION; each component's accepted API/control contracts. Final audit follows SKYBUILD-KEEPER-ADOPTION.
- Brief: move remaining orchestration/runners/resources, readers/collectors, admin/sync/scanners, deployment/units, generic skills/docs and tooling tests; retain product adapters and product tests. Reuse working scripts/miner and consolidate a few valuable repeated operations.
- Acceptance: every census entry moved/adapted/retired; caller versions pinned; no independent writable/launching build stack in SkyKeep; basic progress/failure evidence works; tooling checks and cross-repository adapter compatibility pass.
- Architecture: sections 2 and 9–10. Plan: area 7. Complete extraction is required scope, not deferred multi-project work.

## SKYBUILD-BACKUP-RESTORE — Restore PostgreSQL and rebuild from journal artifacts

- Status: proposed. Priority: low. Area: recovery/testing. Dependencies: SKYBUILD-DAILY-DB-BACKUP artifact; choose the journal/artifact contract as the task’s first step and use an isolated disposable target.
- Brief: create and test database restore and rebuild/start the pinned SkyBuild service from source/artifacts in an isolated environment with production worker launches disabled. Implement the minimal artifact journal and periodic commit writer needed for the drill. Journal location and commit interval remain undecided. Reuse existing event/task/artifact history when suitable; define stable IDs, recoverable content/references and a correct dump-to-journal boundary. Preserve this stable task ID when moving from the deferred ledger.
- Acceptance: reproducible evidence builds/starts the pinned service in a disposable environment and restores covered tasks/history/Cord/artifacts; repeated replay is safe; missing/corrupt/uncommitted artifacts produce explicit recovery limits. Reconcile stops, approvals, usage, claims and live effects without automatically running tasks/models or duplicating completed effects. Keep application/production databases untouched during the drill.
- A30 refinement: restore starts with a new authority epoch and quarantined old credentials/permits; reconcile surviving effects and current restrictions before resuming. Recover immutable task history without replaying its actions or rewriting historical entries.
- Architecture: sections 4, 7–9 and 16. Plan: daily database backups and later recovery. ADR: [0027](../adr/0027-daily-dumps-and-journal-recovery.md). Low priority, not a bootstrap or daily-task prerequisite.
