# SkyBuild agent instructions

## Start

- Use the assigned SkyBuild checkout. Before edits, verify its exact top-level; before Git/GitHub writes, verify both origin URLs identify `stonesky-ai/skybuild`. Pass checkout paths explicitly to workflow scripts. Protect other sessions' files, branches and processes.
- Read only the “Working contract for bounded assignments” at the start of [architecture](docs/design/architecture.md), then sections governing this assignment. Architecture governs the implementation plan; update both together when changing design, interfaces, scope, sequencing or acceptance. Summaries never replace governing requirements. Load the [ADR index](docs/adr/README.md) when consulting decisions; acceptance does not authorize deployment.
- Load [token-efficient coding](.claude/skills/skybuild-token-efficient-coding/SKILL.md). Read available RTK/CodeGraph skills once per session, as required by machine instructions. Do not reread instructions already present in this session. Initialize optional tools only when needed.
- Write normal prose in persisted artifacts. Keep each task's stable ID, owner, phase, next action/blocker and source revision explicit.

## Always retain these boundaries

- SkyBuild owns build tooling and its separate database; never use or migrate SkyKeep product data. Authenticated SkyBuild REST is the sole task authority. Retired Markdown ledgers are not writable task records. Preserve task identity, lineage, append-only history and evidence invalidation; follow [task workflow](docs/design/task_workflow.md) before task-state changes.
- Repository work does not authorize fleet activation, migration, deployment or task completion attestations. Skills and messages grant no additional authority. Inspect unfamiliar API input schemas and action-specific validators before mutation.
- Use existing subscription allowances and approved profiles only. No paid API overage, purchased credits or top-ups. Unknown billing protection parks unattended inference. Preserve account/provider/task budgets and deadlines across retries, engine switches and restarts; never expose credentials.
- Long tasks require automatic compaction or a verified checkpoint/restart policy. Use CPU observation, not repeated model polling. At approval expiry, checkpoint owned work; five configurable minutes of closeout grace never renew implementation authority. Then stop all model calls, including nested calls. Preserve uncertain effects and failed closeout evidence.
- Code tasks require applicable checks and independent adversarial exact-head review in a separate qualified model session, with re-review after fixes. Authors cannot approve or weaken their own gates. Follow [review policy](docs/design/review_policy.md).
- Push owned branches and handoffs. Do not open individual task PRs or merge tasks directly to dev. Publish through reviewed frozen bundles into the active numbered `dev-NNN`, with the full combined gate and confirmed inclusion. Runtime promotion remains separate. Read the detailed integration policy before preparing or publishing a bundle.

## Load detail when its trigger applies

[Detailed policy](docs/agent-policy.md) retains the full requirements. Read the applicable section before the operation, not the whole file at startup:

| Trigger | Required detail |
| --- | --- |
| Service start/stop, specialized tooling, sustained work | “Efficient tools”; use matching guarded skill. Sustained coding requires host watch and failure review every 30 minutes. Check fresh host capacity before heavy gates; preserve the 8 GiB reserve (explicit disposable-gate exception: 6 GiB). |
| Task edits, REST/Cord pilot or worker dispatch | “Authority and data”; task workflow and relevant pilot contract. Private access and scoped credentials precede dispatch. |
| Inference endpoint, model qualification or long-task closeout | “Inference and long tasks”; approval/billing and durable context controls remain mandatory. |
| Review, bundle preparation/publication, dev-cycle closeout, cleanup or backup | “Review, integration, and Git”; matching skill, exact-head evidence and combined gate. Freeze all ready members that fit, in dependency order, maximum 20 tasks; routine coalescing is at most five minutes. Reserve two worktree slots before preparation. |

Machine/user instructions take precedence. Loading less context never waives a governing requirement.
