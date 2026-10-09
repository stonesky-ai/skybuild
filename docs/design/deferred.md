# SkyBuild deferred tasks

Authority: Git-backed planning ledger, revision A36, 2026-10-09. API cutover has not occurred. Stable IDs occur in only one ledger. Deferral means deliberate postponement, not implementation failure. See [architecture](architecture.md) and [mastertodo](mastertodo.md).

## SKYBUILD-MULTIPROJECT — Concurrent autonomous operation across products

- Status: deferred. Area: project operation.
- Brief: enroll additional repositories and test concurrent project claims/worktrees/resources, authorization isolation and budget fairness. SkyKeep is the first named candidate: define how SkyBuild receives its repository, project task authority, build/test commands and scoped credentials without importing or mutating SkyKeep's live task queue or application database.
- Reason: first establish the bootstrap, full extraction and controlled single-project operation. Immutable project IDs and basic isolation are required earlier.
- Revisit: controlled adoption and extraction audit accepted; the owner selects SkyKeep as the first additional project and its repository, task authority and authorized workload are ready.
- Acceptance: cross-project denial/isolation and shared-resource fairness demonstrated without duplicated platform logic.
- Architecture: sections 2, 8 and 11.

## SKYBUILD-METRICS-AUDIT — SkyTrends and SkyAudit exports

- Status: deferred. Area: external integration.
- Brief: reuse existing Prometheus/exporter work and publish versioned metrics/audit streams to the separate products.
- Reason: neither external service should be a prerequisite for task authority or start/stop controls.
- Revisit: stable control/run events exist and consumer requirements are known.
- Acceptance: replay/isolation/version compatibility; loss of an exporter does not change control authority.
- Architecture: sections 2 and 9.

## SKYBUILD-RICH-CONSOLE — Expanded dashboards and analysis

- Status: deferred. Area: visibility.
- Brief: richer historical trends, coverage/lint exploration and interactive multi-project views.
- Reason: task editing/workflow/journal, next actions/blockers, cached status, control provenance, basic progress/errors, existing result artifacts and the task-linked owner-interview/resume panel belong in core scope; visual polish and deeper analysis can follow.
- Revisit: basic console/evidence contracts are accepted and owner use identifies a concrete gap.
- Acceptance: views use the same authoritative records, expose completeness/freshness and do not spawn diagnostic storms.
- Architecture: sections 9–10.

## SKYBUILD-KEEPER-SIMPLIFY — Reassess the execution loop

- Status: deferred. Area: execution internals.
- Brief: simplify or replace parts of Keeper only where observed behavior and costs justify it.
- Reason: adaptation preserves working capabilities and limits rediscovery. A rewrite is not an accepted initial decision.
- Revisit: control/client adoption and behavioral tests are stable; a bounded proposal names a demonstrated problem.
- Acceptance: retained capabilities and stop/update/ownership guarantees remain intact with measured benefit.
- Architecture: section 10.

## SKYBUILD-PREBUNDLE-PRECHECK — Prepare reviewed tasks before integration

- Status: deferred. Priority: conditional. Area: integration throughput. Dependencies: SKYBUILD-BUNDLED-INTEGRATION, exact-head review evidence and qualified gate/publication contracts.
- Brief: adapt useful SkyKeep bundler marshall and `seam_bundle.py` logic to group reviewed ready task branches by dependencies and overlapping files. Against a pinned target base, prepare a candidate, identify conflicts, run applicable cheap checks and hand the integrator a reproducible manifest with member heads, base, candidate, policy, environment, results, exclusions and reasons. Keep final publication with the integrator.
- Reason: find failures while integration is occupied and shorten its serial path. Core MVP bundling does not depend on this optimization. Do not copy SkyKeep host paths, lane exclusions, capacity limits or authority assumptions without current source inspection.
- Revisit: the self-building MVP is running **or** at least 11 integration-ready tasks wait simultaneously. Backlog can trigger earlier selection, not make this an MVP prerequisite.
- Acceptance: stale heads/base/policy invalidate evidence; conflicts and failures have actionable owners. The integrator revalidates freshness and runs the final full combined gate. New arrivals do not alter a frozen candidate; retries or competing preparation cannot create duplicate integration or stale acceptance. Measure preparation and integrator wait time. Reuse checks only for equivalent inputs, environment and trusted results.
- Architecture: sections 2, 8–10 and 13. Plan: follow-on to area 5a. ADR: [0028](../adr/integration.md#adr-0028).

## SKYBUILD-LAPTOP-HA-DR — Optional second-laptop hot failover

- Status: deferred. Priority: very low. Area: availability/disaster recovery.
- Brief: allow another laptop to be configured as a hot-failover HA/DR host for SkyBuild's control service and dedicated PostgreSQL data.
- Reason: explicit owner priority is after core capabilities. Initial laptop operation must not depend on replication, leader election or a second host. Daily PostgreSQL dump commits are planned separately; restore/journal rebuild validation is a proposed low-priority task. Neither introduces an initial HA/DR requirement.
- Revisit: bootstrap, migration, controls, adoption and core extraction are useful and stable; the owner has a second suitable laptop and wants availability improvements.
- Acceptance: agreed recovery time/data-loss targets; tested one-active-controller fencing, replicated/recoverable state, in-flight task/message reconciliation and safe failback under partition. No simultaneous writable authorities or automatic duplication of offline tasks.
- Architecture: sections 3, 8 and 16; ADR 0007. Detailed protocol remains deferred.

## SKYBUILD-COST-AWARE-JOB-TRANSFER — Move portable work off paid hosts

- Status: deferred. Area: placement/host economics. Dependencies: controlled execution, durable checkpoints/result reporting and accepted transfer ownership contract.
- Brief: design resumable subtasks and portable application checkpoints so a last remaining task can move from a rented host to a suitable available laptop, allowing the paid host to stop. Anticipate likely placement/transfer needs when assigning work; respect laptop resources, owner use and scheduled offline windows. Expose the recommendation and owner override in the control panel. The owner's $8/hour, 32-core, 64-GB host is an example, not a selected rental.
- Reason: advanced optimization after core execution and reliable checkpoint recovery. Preserve portable task/checkpoint identifiers and pinned input/artifact references now; do not build scheduling/transfer automation in the bootstrap.
- Revisit: reliable CPU task reporting and checkpoint recovery exist, and measured rental-tail costs justify transfer work.
- Acceptance: compare avoided rental cost against transfer/setup, rework and delay; verify destination capability/access, consistent portable state, source fencing, one destination owner, lost-ack recovery and shutdown only after accepted transfer/no other host work. Owner unavailability overrides remain bounded by approved cost/runtime limits. No model polling or silent GPU/model substitution.
- Architecture: sections 9 and 15. Plan: deferred follow-on in capability/lifecycle research. ADR: [0020](../adr/hosting-recovery.md#adr-0020).

## SKYBUILD-BURST-CAPACITY-PLANNING — Schedule transient capacity and cloud batches

- Status: deferred. Area: capacity/placement and deadline planning. Dependencies: controlled execution, CPU result/checkpoint contracts and qualified cloud lifecycle; advanced backend selection follows SKYBUILD-COMPUTE-ECONOMICS-SPIKE.
- Brief: configure recurring and one-off box availability, household reservations and owner overrides; forecast ready backlog against a desired completion time. Compare local/transient work with a bounded GCloud or other qualified burst's finish time and total cost. Stage a pinned workload, fill useful CPU capacity, drain/preserve results and shut down once that workload is complete. A Thursday-reserved laptop and hour-or-two/32-minute bursts are owner examples, not live configuration or billing guarantees.
- Reason: advanced optimization after reliable execution, observation and lifecycle controls. Keep calendars distinct from actual health/resources; preserve the required task/capacity interfaces without making an optimizer a bootstrap prerequisite. Core task bundling is required earlier under SKYBUILD-BUNDLED-INTEGRATION; only capacity/calendar/cloud optimization is deferred.
- Revisit: measured backlog/deadline needs justify paid batching and the economics spike has qualified suitable backends.
- Acceptance: verify calendar/offline overrides, uncertain forecasts, dependency/inference-blocked backlog, serialized integration/merge ownership, resource bounds, pinned workload scope and money/runtime caps. New work cannot silently extend a burst. Results survive drain/shutdown; provider stop is confirmed with an independent backstop; model review can wait after CPU results are saved. CPU status and planning do not require model polling.
- Architecture: sections 9 and 15. Plan: deferred follow-on in capability/lifecycle research. ADR: [0021](../adr/hosting-recovery.md#adr-0021). Portable tail-task transfer is complementary, not a prerequisite for every burst.

## SKYBUILD-COMPUTE-ECONOMICS-SPIKE — Measure box pools, bursts and serverless

- Status: deferred. Area: research/experiment. Dependencies: representative pinned CPU-ready workload and result/checkpoint contract; cloud testing requires a separately selected experiment money/runtime cap and qualified credentials/lifecycle.
- Brief: investigate configured machine pools, rented VM bursts and suitable managed-batch/serverless runners. Research current primary pricing/capability constraints, compare estimated completion/cost, then run the same bounded accepted workload locally and on at least one qualified remote candidate. Actual serverless suitability and financial benefit are unresearched.
- Reason: the owner explicitly requested a later spike and test before choosing the advanced architecture. Recording it is not permission to provision, spend or benchmark now.
- Revisit: advanced compute planning is selected and representative work, experiment scope and caps are agreed.
- Acceptance: produce reproducible workload/source/settings, measured completion and total cost per accepted workload, including startup/staging, idle/serial work, transfer/storage, retries and verified shutdown/cancellation. Compare estimates with observed charges and mark untested options/uncertainty. Evaluate runtime/resources, Git/artifact access, durable state, cancellation and checkpoint portability; make an evidence-based backend recommendation. No production mutations or paid inference merely to benchmark CPU economics; confirm test resources are stopped.
- Architecture: section 15. Plan: deferred prerequisite for advanced backend selection; initial laptop hosting, core reporting and full extraction remain independent. ADR: [0021](../adr/hosting-recovery.md#adr-0021).

## SKYBUILD-NETWORK-PROVIDERS — Configure an alternative private VPN

- Status: deferred. Area: network configuration. Dependencies: useful initial Tailscale website/API service and an actual alternative-provider requirement.
- Brief: make private-network choice configurable later while preserving the standard HTTPS endpoint, client/authentication contract, project scopes and local recovery. Tailscale is fixed for the initial design.
- Reason: the owner explicitly deferred provider configurability; a generic VPN management layer adds no bootstrap value.
- Revisit: another deployment needs a different private network and its reachability/identity/lifecycle constraints are known.
- Acceptance: document and validate the chosen alternative, endpoint/certificate changes, access rules, credential revocation and outage/recovery behavior. A provider change cannot silently expose the service publicly or grant application privileges.
- Architecture: sections 3 and 5. Plan: later network configuration, independent of initial bootstrap. ADR: [0023](../adr/hosting-recovery.md#adr-0023).

## SKYBUILD-GIT-HOSTS — Later GitLab and Bitbucket support

- Status: deferred. Area: repository integrations.
- Brief: support project remotes and PR/merge lifecycle on GitLab and Bitbucket after the initial GitHub workflow is useful and an alternative is selected.
- Reason: all projects initially presume GitHub; generic multi-host support is unnecessary for bootstrap.
- Revisit: the owner selects a real GitLab/Bitbucket project and authorizes that integration.
- Acceptance: provider-specific credentials, push/PR/merge state and higher-branch cleanup semantics are qualified without weakening task ownership or misreporting remote preservation. Preserve ordinary Git/client contracts.
- Architecture: sections 2 and 8. Plan: deferred repository/recovery work. ADR: [0026](../adr/integration.md#adr-0026).

## SKYBUILD-TEAM-APPROVAL-CHAIN — Multi-person team approval workflow

- Status: deferred. Priority: very low. Area: team collaboration and approvals.
- Brief: add configurable approval chains for multiple people cooperating on a build, with appropriate reviewer/approver roles and a visible next approver/action. Preserve actor identity, immutable journal history and the current task/definition version being approved.
- Reason: explicit owner direction is much later. Initial owner/worker access, technical review, task page and automatic integration must not depend on a multi-person approval engine.
- Revisit: core SkyBuild is useful and stable, and the owner selects a real multi-person collaboration requirement.
- Acceptance: qualify the chosen chain and delegation/expiry behavior; scope or definition changes invalidate affected approvals, concurrent responses cannot skip required stages, and historical decisions remain append-only. No approval silently raises budgets, renews authority or bypasses required gates.
- Architecture: section 5 and [task workflow](task_workflow.md). Plan: future team collaboration, outside initial delivery. ADR: [0031](../adr/tasks.md#adr-0031).

## SKYBUILD-USER-LOGIN — Design and implement real Workbench user accounts

- Status: deferred. Area: identity and access.
- Brief: replace the Workbench local-preview demo identity (`user1` / `abcd1234`) with real SkyBuild user accounts, password verification, browser sessions, logout/revocation and account recovery. Review the browser-to-API contract, including whether the current bearer-token API remains behind a server session or changes. Preserve project and operation scopes, derive journal actors from authenticated identity, and keep worker credentials separate from human accounts.
- Reason: owner wants a fake always-signed-in account while building the Workbench. Real credential and session design is deferred until a dedicated design review. The demo credentials and local fake tasks grant no API or database access.
- Revisit: owner starts design review or before Workbench connects to live task data for routine use.
- Acceptance: design review records session, password storage/reset, CSRF, cookie, revocation, audit-actor and API-token compatibility decisions; implementation replaces the demo identity without weakening project scopes or enabling unauthenticated task access. Browser task reads and writes require the approved user session, and worker/service credentials remain separately scoped.
- Architecture: sections 3–5. Plan: identity design review before live Workbench access.

## SKYBUILD-QUALITY-GATES — Configurable complexity and compactness review gates

- Status: deferred. Priority: early after MVP. Area: review quality.
- Brief: add pinned Ruff checks, including McCabe `C901`, to the code-review path; measure the existing repository and retain the CPU-first complexity/size profile, metric deltas, GUI thresholds/severity, scoped baselines/exceptions and versioned refactoring prompts. Default proposal: McCabe complexity 10, function-size warning above 50 statements; qualify/pin the tools before enforcement.
- Reason: the owner explicitly wants the running self-building parallel-worker MVP first. Independent review and existing required checks remain in MVP; this richer feature does not block it.
- Revisit: SKYBUILD-SELF-BUILD-MVP is running; select this as a feature SkyBuild can build for itself.
- Acceptance: actionable findings and test requests, complete existing-repo inventory, meaningful size/complexity measurements, immutable dispositions and policy-change invalidation; no self-weakened gates, metric gaming or unbounded refactor loops. Preserve required behavior and tests. Hand actual debt findings to SKYBUILD-QUALITY-DEBT-CLEANUP; a baseline is not permanent debt acceptance.
- Architecture: section 13 and [review policy](review_policy.md). Plan: early post-MVP follow-on. ADRs: [0033](../adr/quality.md#adr-0033), [0034](../adr/quality.md#adr-0034).

## SKYBUILD-QUALITY-DEBT-CLEANUP — Clear initial Ruff and McCabe debt

- Status: deferred. Priority: early after MVP. Area: review quality.
- Dependencies: running SKYBUILD-SELF-BUILD-MVP and the initial SKYBUILD-QUALITY-GATES repository scan.
- Brief: turn every actual Ruff/McCabe finding in the initial repository baseline into bounded cleanup work; refactor without deleting required behavior or useful tests. Track source, rule, severity, owner, correction and accepted publication for each finding.
- Reason: the owner wants this among the first post-MVP tasks, while keeping the MVP itself unblocked by a broad cleanup.
- Revisit: the quality scan has produced its pinned, complete finding inventory.
- Acceptance: every actual debt finding has a published fix with applicable tests and independent review; verified false positives or non-debt exceptions have narrow authorized dispositions. No inherited debt remains hidden behind the baseline.
- Architecture: section 13 and [review policy](review_policy.md). Plan: early post-MVP follow-on. ADR: [0034](../adr/quality.md#adr-0034).

## SKYBUILD-REPO-REVIEW-CADENCE — Serial whole-repository review

- Status: deferred. Priority: early after MVP. Area: review quality.
- Dependencies: running SKYBUILD-SELF-BUILD-MVP, SKYBUILD-QUALITY-GATES and initial SKYBUILD-QUALITY-DEBT-CLEANUP.
- Brief: schedule a background whole-repository code review when either 1,000 accepted commits or 5,000 changed source-code lines accrue since the last completed review, whichever happens first. Persist counters, review identity, exact source baseline, findings and backlog. Expose both thresholds in the GUI in a later settings slice.
- Reason: the owner wants periodic broad review without overlapping review runs or repeatedly rediscovering unmerged fixes.
- Revisit: initial quality debt is cleared and the controlled review path can run background tasks within its budgets.
- Acceptance: at most one whole-repository review is active. The next review waits until all actionable findings from the prior review have accepted published fixes. Accidental duplicate starts and findings coalesce under the same durable identity; a lost acknowledgment or unknown run does not release the hold. Crossing either threshold while held leaves a visible pending review, not a second launch. CPU threshold detection never grants model-spend authority.
- Architecture: section 13 and [review policy](review_policy.md). Plan: early post-MVP follow-on. ADR: [0034](../adr/quality.md#adr-0034).
