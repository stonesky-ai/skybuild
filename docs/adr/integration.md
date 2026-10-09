# Git and integration decisions

Each section retains its original ADR ID. Status applies to the decision, not implementation. [Architecture](../design/architecture.md) governs current requirements.

<a id="adr-0026"></a>
## ADR 0026: GitHub delivery and scoped Git lifecycle

**Accepted GitHub, task-owned reset/clean and confirmed post-merge cleanup; checks proposed, 2026-10-08.** Push owned source/docs and committed checkpoint/handoff artifacts to configured GitHub repositories. Record repo, branch, exact commit and confirmed or pending remote push. A push does not merge, deploy, export database state or renew authority. Defer GitLab/Bitbucket.

Allow reset/clean in task-owned worktrees and branch/worktree cleanup after confirmed merge into the intended target. Do not impose a blanket destructive-command ban or require a new prompt solely for an authorized destructive Git operation. Protect other workers and shared refs. Check worktree and ref ownership, intended base and actual PR outcome; closed-unmerged is not merged, and squash/rebase or post-merge commits need content-aware proof. Preserve local evidence on failed push. Architecture sections 2 and 8.

<a id="adr-0028"></a>
## ADR 0028: Automatic reviewed bundles

**Accepted automatic integration and core bundling; limits/mechanism proposed, 2026-10-08.** Collect ready reviewed tasks as a bundle. Freeze members, exact heads, dependencies, base, accepted policy and combined candidate; run required combined gates and publish only the passed result within existing authority. Arrivals wait for the next bundle. No per-task full suite or fresh owner merge prompt is required by default. Deployment/promotion keeps separate controls.

Use one fenced integrator per project/target. Required gates are the **union** of member and project checks; deduplicate only equivalent checks or explicit coverage, per [ADR 0030](#adr-0030). Integration-only edits need new review/candidate. Changed heads/base require new evidence. Confirm remote publication and each task's inclusion before acceptance or cleanup; closed PR alone proves nothing. Failed bundles remain unintegrated; diagnose/split under bounded authority and retest changed subsets. Reconcile uncertain publication, cap queued resources and surface CPU-observed backlog/throughput. Bundle size/age, failure limits and publisher mechanism remain open. Architecture sections 2, 8–10, 13 and 15.

<a id="adr-0030"></a>
## ADR 0030: Effect boundaries and accounting

**Proposed audit refinements, 2026-10-08; existing owner policies remain authoritative.** Apply these boundaries without adding a broker or generic workflow engine:

- Take the union of required checks. An explicit coverage rule, not a presumed strongest profile, may replace one check with another.
- Freeze accepted gate policy outside candidate execution. Keep publication credentials and accepted-check authority away from candidate code.
- Record durable unresolved publication intent. A trusted publisher must enforce tested head/base at the external write and reconcile unknown outcomes before releasing the slot. GitHub's merge `sha` guards PR head, not expected base; a prior local read cannot supply base protection. Qualify branch protection, check identity, bypass rights and actual merge behavior. An enqueued request is not a confirmed merge.
- Subdivide reservations and settle each model operation once across parent, child and bundle tasks. Retain uncertain exposure and whole-task history; return only proven unused allocation.
- Use exclusive offline allocations and suspend-aware deadlines, or park until reconciliation. Restores need a new authority epoch and quarantine of surviving external effects.

The [publication model](../design/models/Publication.md) has bounded TLC simulation evidence, not proof of GitHub behavior or implementation. Exact publication flow, cost allocation and offline bounds remain open. Runtime deployment remains separately controlled. Architecture sections 2, 7–8 and 13.
