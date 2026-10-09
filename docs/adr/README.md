# Architecture decision records

[architecture.md](../design/architecture.md) governs SkyBuild. Planning documents and the initial task ledgers live in docs/design; ADRs live here. An ADR preserves the context, decision and consequences of a significant architectural choice. Accepted decisions must be reflected in architecture.md and their implications in implementation_plan.md in the same revision.

Use sequential filenames NNNN-short-title.md. Status is proposed, accepted, rejected or superseded; superseded records link their replacement. Accepted means the design choice is approved, not that its implementation is done or deployment authorized. Do not rewrite history to disguise a changed choice: record the replacement and reconcile the current architecture. Minor implementation details need no ADR.

| ADR | Status | Decision |
| --- | --- | --- |
| [0001](0001-product-and-database-boundary.md) | Accepted, owner direction 2026-10-08 | Dedicated SkyBuild repository, all-tooling extraction and separate PostgreSQL database. |
| [0002](0002-task-bootstrap-and-authority.md) | Accepted direction; mechanics proposed | Three task ledgers until a PostgreSQL REST task service and minimal communications exist; one explicit authority switch. |
| [0003](0003-architecture-governs-planning.md) | Accepted, owner direction 2026-10-08 | Architecture governs; implementation planning follows and splits by area only as needed. |
| [0004](0004-controlled-launch-boundary.md) | Proposed | Deterministic admission, fenced ownership, restrictive controls and acknowledged physical stop boundary. |
| [0005](0005-compact-cpu-first-builder.md) | Proposed mechanics; owner goals confirmed | Compact components, CPU-first work, qualified inference, separate host lifecycles and stable self-build control. |
| [0006](0006-laptop-first-control-host.md) | Accepted, owner interview | Initial API and dedicated PostgreSQL on the owner's laptop; discuss constraints before moving. |
| [0007](0007-outage-policy-and-deferred-failover.md) | Accepted default/UI and deferral | Finish-current-task outage default, website alternative, very-low-priority second-laptop HA/DR. |
| [0008](0008-task-allowance-and-approval-threshold.md) | Accepted weekly basis/threshold/UI; accounting proposed | Full weekly Claude allowance percentage task budgets; no task approval strictly below the configurable 1% default. |
| [0009](0009-timed-approval-and-daily-provider-caps.md) | Accepted eight-hour default, interval/daily caps and Claude default; mechanics proposed | Configurable eight-hour approval with an overall interval cap; daily caps per frontier provider; Claude defaults to 10% of full weekly allowance per day. |
| [0010](0010-frontier-capacity-reserve.md) | Accepted reserve goal and basis; mechanics proposed | Slow near 90–95% total provider allowance consumed, including non-build use, to preserve urgent frontier-model capacity. |
| [0011](0011-central-usage-readings.md) | Accepted central/model-level visibility; mechanics proposed | One cached REST usage surface and collector; preserve actual shared pools and unknown readings. |
| [0012](0012-expiring-model-capacity.md) | Accepted expiry preference; mechanics proposed | Prefer useful qualified work on soon-expiring model capacity; reserve exceptions remain open. |
| [0013](0013-configurable-coordinator-engine.md) | Accepted engine choice; automatic policy resolved in 0022; mechanics proposed | Configurable Codex/Claude coordinator, durable portable state and controlled engine handoff. |
| [0014](0014-multiple-provider-accounts.md) | Accepted accounts/98% rotation; mechanics proposed | Multiple named accounts per provider, isolated credentials and budget-preserving account rotation. |
| [0015](0015-subscription-only-billing.md) | Accepted restriction; enforcement qualification open | Existing subscription allowances only; switching cannot enable API charges, purchased usage-credit overage or top-ups. |
| [0016](0016-long-task-context-and-monitoring.md) | Accepted long-task requirement; numeric settings/qualification open | Per-task compaction or verified restart policy, CPU monitoring and bounded model wakeups. |
| [0017](0017-pooled-reserve-and-account-rotation.md) | Accepted pooled reserve; qualification/threshold details open | Urgent reserve across comparable accounts together, with individual account rotation near 98%. |
| [0018](0018-pre-limit-worker-drain.md) | Accepted proactive drain; numeric margins open; expiry resolved in 0019 | Drain before approval time or usage limits to minimize unfinished work. |
| [0019](0019-expiry-handoffs-and-model-cutoff.md) | Accepted closeout/five-minute configurable grace/query cutoff and owner questions; mechanics open | Compact/commit/attach resume handoff at expiry, cut off model queries after grace, and let authorized CPU tasks report independently. |
| [0020](0020-deferred-cost-aware-job-transfer.md) | Accepted later capability; implementation deferred | Portable subtasks/checkpoints and cost-aware transfer off paid hosts, with laptop availability and owner override. |
| [0021](0021-deferred-capacity-calendars-and-bursts.md) | Accepted later capabilities/spike; implementation deferred; backend unselected | Transient capacity calendars, bounded workload bursts and a measured box/VM/serverless economics comparison. |
| [0022](0022-automatic-coordinator-failover.md) | Accepted automatic failover; adapter/ownership qualification open | Switch between qualified allowed Codex/Claude subscription profiles within existing approval and budgets. |
| [0023](0023-initial-tailscale-access.md) | Accepted initial network/reachability; exposure mechanics proposed | Tailscale website/API access from all intended enrolled boxes; other VPN configuration later and application privileges separate. |
| [0024](0024-owner-and-worker-credentials.md) | Accepted owner/worker split; credential/session mechanics proposed | Owner/admin credentials and separate project-scoped worker tokens; workers cannot expand permissions or approve spending. |
| [0025](0025-deferred-backup-and-restore.md) | Superseded by 0027 | Defer daily encrypted off-laptop/pre-migration backups and restore work; no bootstrap/cutover/migration gate. |
| [0026](0026-github-remotes-and-git-lifecycle.md) | Accepted GitHub and scoped Git lifecycle; enforcement checks proposed | GitHub delivery, owned worktree reset/clean and confirmed post-merge cleanup; protect other workers/shared branches without blanket command bans. |
| [0027](0027-daily-dumps-and-journal-recovery.md) | Accepted daily dump/commit and low-priority restore/rebuild; journal contract open | Scheduled PostgreSQL dump-file commits, plus tested recovery from periodically committed journal artifacts at low priority. |
| [0028](0028-automatic-bundled-integration.md) | Accepted automatic merge and core task bundling; mechanics proposed | Freeze reviewed tasks into integration bundles, gate the combined candidate and automatically publish its verified result. |
| [0029](0029-canonical-task-terminology.md) | Accepted canonical terminology; legacy mappings need inspection | Task is the work unit; new API uses `/tasks`; preserve exact legacy identifiers/history and one task identity. |
| [0030](0030-effect-boundaries-and-accounting.md) | Proposed audit refinements; adapter qualification open | Gate union, external publication enforcement, explicit allocation accounting, offline deadlines and recovery authority epoch. |
| [0031](0031-task-workflow-and-append-only-journal.md) | Accepted workflow/page/append-only history and later team approvals; mechanics proposed | Every task has a known next action; editable task scope and structural operations have immutable history; multi-person approval chains are deferred. |
| [0032](0032-shared-inference-capacity.md) | Accepted shared free inference and visible tradeoffs; capability/limits need qualification | Separate Qwen 4, Recall 2 and BGE-M3 1 reported pools; bounded coding, shared test capacity and integration-aware dispatch. |

| [0033](0033-independent-review-and-compactness.md) | Accepted independent review and post-MVP quality feature; defaults/mechanics proposed | Separate adversarial review with actionable fixes/tests; retain configurable complexity/size criteria after the self-building parallel-worker MVP. |

For a new record, state date/status, context, decision, alternatives, consequences, governing architecture sections and any unresolved choices. Keep it concise enough to explain why the current design exists.
