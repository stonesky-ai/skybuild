# Project dependency graph toolset — task design handoff

**Purpose:** Plan a new SkyBuild task for building a mostly deterministic tool that maps project task dependencies and exposes the path to a chosen milestone. This document is a design handoff for a later session. It does not add a ledger task, approve implementation, or authorize runtime changes.

## Context and discovery method

The originating discussion asked whether a project's critical path can be mapped deterministically from its tasks, how the SkyBuild MVP path was derived, and whether this can be scripted or summarized in a model skill. The follow-up clarified that SkyBuild's MVP itself is recorded as a task.

I used the SkyBuild checkout at `/home/kevin/my_code/skybuild`, branch `dev-002`. At the time of discovery the working tree was clean. I read the project instructions and governing project documents, then compared the task ledger and plan:

- `AGENTS.md`: architecture governs implementation planning; task authority currently resides in Git-backed ledgers; unfinished tasks need a next action or named blocker; stable task IDs must appear in exactly one ledger.
- `docs/design/architecture.md`: sections 1 and 5 describe task authority and task contracts; section 16 defines the self-building MVP. Architecture revision was A40.
- `docs/design/implementation_plan.md`: “MVP delivery target” and “Shortest implementation path” describe the sequence and the required evidence. Header identified A39 as source architecture, so reconcile this revision mismatch before treating it as current planning authority.
- `docs/design/mastertodo.md`: contains `SKYBUILD-SELF-BUILD-MVP` and its prerequisite task IDs, plus current task states and blockers. The inspected ledger header identified revision A38; reconcile with newer architecture before editing records.
- `docs/design/task_workflow.md`: task dependency edits are cycle-checked, journaled, and cause reassessment; unfinished tasks carry phase, next action/resolver, blocker and evidence freshness. Milestones initially reference an accepted task or explicit owner-recorded event.

The MVP path previously given was assembled by starting at the MVP acceptance statement, identifying the systems needed to satisfy it, then tracing the declared task dependencies and implementation order backward. The resulting summary was:

`SKYBUILD-BOOTSTRAP` → `SKYBUILD-TASK-CUTOVER` → minimal `SKYBUILD-TASK-WORKBENCH` and `SKYBUILD-EXECUTION-CONTROLS` → `SKYBUILD-KEEPER-ADOPTION`, one qualified author/reviewer route, and `SKYBUILD-BUNDLED-INTEGRATION` → `SKYBUILD-SELF-BUILD-MVP` acceptance demonstration.

The middle tasks include parallel branches; the arrow notation is a readable sequence summary, not a claim that every middle item depends serially on every prior item. The relevant task ledger records explicit dependencies. The MVP acceptance itself is an ordinary task-shaped record used as a milestone/acceptance task; it depends on prerequisite tasks and contains the end-to-end demonstration criteria. It must not be mistaken for a runtime subsystem or for an automatically derived project milestone.

Current statuses and blockers change over time. Before implementation or a live status report, the next session must reread the current ledger and plan rather than rely on this historical snapshot. At discovery, bootstrap had local test/review evidence but incomplete deployment qualification; cutover had no imported tasks/authority receipt and a fail-closed write-gap action; workbench had a thin UI slice; controls, Keeper adoption, and bundled integration had remaining work. Exact state must be reconfirmed.

## Key distinction: dependency path versus schedule critical path

The tool should use precise terminology:

1. **Dependency path:** the transitive prerequisites needed to reach a target task/milestone, based on declared dependency edges. This is mostly deterministic given a task snapshot.
2. **Blocked/ready path view:** which prerequisite tasks are incomplete, blocked, or eligible for action, based on task state and acceptance evidence. This can be computed from the task snapshot and explicit status rules.
3. **Schedule critical path:** the longest-duration path through a dependency DAG, typically used to estimate earliest finish and slack. This requires duration estimates and scheduling assumptions. Shared people, reviewers, workers, model slots, resource limits, uncertainty, and task splitting make it more than a plain DAG calculation.

Do not label the first result a schedule-critical path. If duration/resource data is absent, report dependency-critical candidates and blockers, and state that schedule criticality is unknown.

## Proposed task objective

Create a small deterministic SkyBuild project-dependency analysis tool that reads one frozen task snapshot and a target task or milestone, validates the dependency graph, and emits a concise explanation of prerequisite paths, incomplete blockers, parallel branches, and actionable next tasks. Optionally provide a thin model-facing skill that invokes or consumes this output and summarizes it without changing the graph.

This objective needs owner selection and a stable task ID before insertion into `mastertodo.md`. Follow the repo rule: put that ID in exactly one ledger. Planning does not authorize API migration, live queue edits, model work, or deployment.

## Inputs and source of truth

Use the authoritative task data source selected by the project state:

- Before task API cutover: read a frozen Git-backed ledger snapshot, preserving source commit/hash and stable IDs.
- After cutover: use a versioned, read-only task API/export contract. Do not read the Markdown ledgers as competing authority after they become read-only exports.

The minimum logical task record needed by analysis:

- stable task ID;
- project ID/scope;
- declared dependency IDs;
- status and delivery phase;
- completion/acceptance evidence state or a clear unknown value;
- next action or named blocker/resolver for unfinished tasks;
- deferred trigger when applicable;
- superseded/replacement links when applicable;
- optional estimated duration/range, resource needs/capacity, and confidence for schedule analysis.

Milestone input must identify either a target task whose accepted completion represents the milestone, or an explicit milestone event. Do not infer that a task is a milestone only from its title. For SkyBuild's MVP, use the explicit `SKYBUILD-SELF-BUILD-MVP` acceptance task and its declared dependency records.

Every output should identify the exact input revision, commit/hash or API snapshot/version, target ID, analysis version, and time. Never merge observations from different snapshots into one graph without reporting this.

## Deterministic analysis requirements

For a fixed validated snapshot, target, and algorithm version, results must be reproducible. The core should:

1. Parse records into a directed graph where edge direction is prerequisite → dependent task. If the source encodes the inverse convention, normalize it explicitly.
2. Validate uniqueness and scope of task IDs, dependency references, target existence, self-dependencies, superseded IDs, and project boundaries.
3. Detect cycles and report an actionable cycle path. Never silently break or omit a cycle.
4. Walk backward from the target to find its transitive dependency subgraph. Keep unrelated project tasks out of the target-path result, while optionally reporting orphaned or globally invalid records separately.
5. Classify dependencies using explicit policy: accepted/done, unfinished, blocked, deferred, active, ready, unknown/stale evidence, superseded/replaced. Do not treat a status label alone as proof of acceptance when evidence is absent or stale.
6. Produce a deterministic topological ordering with a stable tie-breaker such as task ID. Preserve the partial order; do not present arbitrary order among parallel tasks as a required sequence.
7. Identify tasks that unlock the most remaining target-path work, while keeping this as a deterministic reachability/count metric rather than a claim about elapsed time or business priority.
8. Report missing next actions/resolvers on unfinished tasks as data-quality defects, consistent with `task_workflow.md`.
9. Explain each reported path with task IDs and direct dependency edges so a person can verify why a task is included.
10. Return machine-readable output and a concise human-readable summary. Include warnings and unknowns as first-class output, not hidden logs.

Suggested core output fields: `snapshot`, `target`, `result_kind` (`dependency_path` or `schedule_estimate`), `nodes`, `edges`, `topological_layers`, `incomplete_prerequisites`, `blocked_branches`, `ready_candidates`, `unknowns`, `cycle_errors`, `data_quality_errors`, and `assumptions`.

## Optional schedule analysis

Keep schedule analysis a separate mode. Only compute longest-path finish estimates when duration estimates and scheduling policy exist. Define whether durations are points or ranges, how unknown duration is handled, calendar assumptions, and whether the result ignores resource contention. If resources are modeled, specify capacity and task resource demand explicitly. Do not imply that a normal DAG longest path accounts for shared reviewer/model/worker bottlenecks.

If duration or resource data is incomplete, return `schedule_estimate: unavailable` or an explicitly bounded estimate; never substitute zero duration silently. Mark all estimates with source and confidence. Do not let estimated schedule urgency reorder authoritative task priority without owner policy.

## Model skill boundary

The deterministic tool owns parsing, graph validation, reachability, ordering, and arithmetic. A concise skill may instruct a model to:

- run the tool on an identified snapshot and target;
- summarize critical dependency chain, parallel branches, current blockers, and next actions;
- cite task IDs and direct edges for each claim;
- distinguish dependency-path facts from duration/resource estimates;
- flag ambiguous acceptance or missing dependencies as proposed questions, not silently add edges;
- preserve unknown status and stale/missing evidence;
- avoid changing tasks, dependency edges, authority, budgets, or execution state.

The model may suggest missing edges with a rationale and evidence, but a human/task-authority workflow must approve and record them. A saved skill is persisted project policy and must use normal prose. Do not introduce a model call for routine deterministic graph refreshes.

## Scope and non-goals

Initial scope: one project, one frozen snapshot, one target milestone/task, dependency validation, path and blocker report, deterministic JSON plus readable text, test fixtures for graph edge cases.

Do not include in first slice unless later task selection requires it:

- automatic task creation or dependency mutation;
- model-based dependency inference as authority;
- live task cutover or ledger import;
- a roadmap service or schedule optimizer;
- workforce/resource-constrained project scheduling;
- GUI changes, notifications, automatic admission, model launch, integration, or deployment;
- broad migration of historical task formats.

Reuse the canonical task and milestone contracts. Do not invent another queue, task identity, or source of truth.

## Suggested implementation sequence

1. **Reconnaissance:** read current `AGENTS.md`, architecture sections 1/5/16, implementation plan, task workflow, ADR index, and current ledgers. Search current source for task models/API/export and existing dependency validation only after following the CodeGraph/RTK instructions. Confirm current branch, dirty state, authority phase, and relevant project skill instructions. Do not assume the records described above remain current.
2. **Contract:** settle the exact snapshot format and target/milestone representation; document dependency edge direction, acceptance freshness semantics, status classification, deterministic tie-breaker, error behavior, and output schema. Add or update architecture and implementation plan together if this changes architecture/interface/scope/dependencies/acceptance, as required by `AGENTS.md`.
3. **Task brief:** write a bounded implementation brief with exact source revision/hash, changed paths, dependencies, acceptance evidence, and correction bound. Add the new stable task ID to exactly one ledger only when task planning is authorized and owner-selected.
4. **Pure deterministic core:** implement parsing/normalization, validation, cycle detection, target reachability, topological layers, blocker/readiness classification, and stable output. Keep I/O adapter thin.
5. **Adapters:** read the current authoritative source safely (frozen ledger or read-only API/export as appropriate). Emit source snapshot metadata. Do not add write paths.
6. **CLI and report:** provide a command that names target and input snapshot, emits JSON and concise text, exits nonzero on invalid graph/data, and does not mutate the source.
7. **Optional skill:** only after CLI/output contract stabilizes, add a concise project skill that guides model summarization. State its boundaries and require evidence-linked statements.
8. **Validation and review:** cover empty graph, simple chain, branches/parallel layers, completed versus incomplete dependencies, blocked/deferred/unknown tasks, missing IDs, duplicate IDs, cross-project edge, self-edge, multi-node cycle, superseded task, stale/missing acceptance evidence, deterministic tie-breaks, malformed input, and snapshot mismatch. Add schedule tests only if that mode is implemented. Follow SkyBuild's applicable checks and independent exact-head review/integration rules.

Do not run tests solely because this handoff is written. The implementation session should run tests required by its selected task and applicable project policy.

## Acceptance criteria for the proposed tool

- Same snapshot, target, and tool version yield byte-equivalent canonical JSON (excluding any explicitly designated non-deterministic metadata field).
- Tool returns exact transitive prerequisites for the target and excludes unrelated valid tasks.
- Valid parallel branches remain visibly parallel; stable sorting does not imply extra dependency edges.
- Invalid references, duplicate IDs, self-dependencies, scope violations, and cycles are reported with actionable IDs and no misleading path result.
- Completion depends on current acceptance evidence under the project's explicit rule; missing/stale evidence remains unknown or incomplete.
- Every unfinished target-path task exposes its next action/resolver or is reported as a data-quality blocker.
- Human summary agrees with machine output and links each claim to IDs/edges.
- Source remains unchanged; no task/API writes, model invocation, launch, or deployment occurs.
- Schedule-critical-path claims appear only when selected inputs and policy support them; otherwise report dependency path only.
- Independent review confirms deterministic behavior, correct error handling, and no unauthorized state mutation.

## Open decisions for the next session/owner

1. Is this a planning task to add to `mastertodo.md`, or implementation of an already owner-selected task? Assign stable task ID if creating it.
2. Should the first input be Git ledger Markdown, a canonical JSON export, or the post-cutover task API? Current authority phase decides the valid answer; do not read two authorities concurrently.
3. What exactly constitutes accepted/done evidence for generic tasks, including CPU/planning tasks that do not need review or merge?
4. Is first release dependency-only, or is there a real duration/resource dataset that justifies schedule critical-path mode?
5. Where should the CLI/output live, and is a model skill wanted in the first task or a follow-on task?
6. What output consumers are expected (human CLI only, future UI/API, or both)? Keep first interface minimal.

## Session continuation checklist

- Start in `/home/kevin/my_code/skybuild`; read applicable instructions and skills, including RTK, CodeGraph, and project token-efficient workflow.
- Check `git status`, branch, current task ledger and current architecture/plan revisions. Preserve existing user changes.
- Verify `codegraph status` names this checkout before any graph query. Respect the no-index/no-init rule in current `AGENTS.md`.
- Verify source authority state before selecting a reader adapter.
- Confirm owner-selected task scope and stable ID before task-ledger mutation or implementation. This document alone is not task-selection or runtime authority.
- Keep the first version read-only and deterministic. Report decisions and unresolved assumptions in the implementation brief.

