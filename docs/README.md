# SkyBuild documentation

Start with the governing [architecture](design/architecture.md), then the derived [implementation plan](design/implementation_plan.md).

The derived [task workflow](design/task_workflow.md) contains the flowchart, action-to-state table, task-page behavior and append-only journal contract.

The derived [review policy](design/review_policy.md) defines mechanical checks, independent adversarial review, actionable findings and compactness criteria.

The temporary task ledgers are [mastertodo](design/mastertodo.md), [deferred](design/deferred.md) and [alreadydone](design/alreadydone.md). They are the task authority until the PostgreSQL API cutover described in the architecture.

[Architecture decision records](adr/README.md) explain accepted decisions and proposed alternatives. [Research snapshots](design/research/README.md) preserve the evidence used for planning; they do not establish current deployment state.

For continuation context, read the [session handoff](design/session_handoff.md).

The former dated combined plan is superseded by architecture.md and implementation_plan.md. Its original text remains in Git history at commit 2da489c992f55e3484e475bdf3a175a97e1d90c7.

For installation-specific inference configuration, copy the root [.env.example](../.env.example) to `.env` and set the endpoint URL and external credential-file path. Local `.env` files are ignored by Git; the token itself stays outside the repository. This is a configuration contract for the planned adapter, not an implemented runtime.
