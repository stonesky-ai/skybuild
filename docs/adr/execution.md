# Execution control decisions

Each section retains its original ADR ID. Status applies to the decision, not implementation. [Architecture](../design/architecture.md) governs current requirements.

<a id="adr-0004"></a>
## ADR 0004: Controlled launch and stop boundary

**Proposed; physical protocol and numeric policies open, 2026-10-08.** Route every managed launcher through deterministic admission. Atomically reserve ownership, concurrency, budget and resources under current restrictive controls. Issue single-use fenced permits and reconcile a durable local launch journal. Hold reservations while execution is uncertain; compose local and central stops so stale enables cannot clear either.

A stop fences new admission immediately, but physical effectiveness for a box requires acknowledgment after in-flight starts reconcile. Show disconnected boxes as pending/unknown. Independently supervised observers supply progress/death evidence without an uncontrolled paid-restart path. The launch-free task/Cord bootstrap needs none of this. Before canary adoption, qualify spend/deadline policies, crash/replay, stop acknowledgment and every launcher bypass. Architecture sections 8–10.

<a id="adr-0007"></a>
## ADR 0007: Controller outage behavior and deferred failover

**Accepted default/UI and deferral; offline bounds proposed, 2026-10-08.** Expose controller-contact-loss policy in the website. Default to finishing the current authorized task; offer checkpoint/stop at the next safe point. An outage permits no new claim, scope expansion or unapproved retry. Carry policy/version with authorization and buffer bounded, idempotent results for reconnect.

Do not treat an expired lease or missed heartbeat as proof a worker stopped. Keep uncertain ownership visible. A disconnected worker may not receive a new stop/policy until acknowledgment; do not claim immediate enforcement. Defer a second-laptop HA/DR controller to very low priority. Later failover needs one fenced writer, recoverable data and in-flight reconciliation. Explicit stops remain separate. Architecture sections 3, 8–9, 12 and 15–16.

<a id="adr-0016"></a>
## ADR 0016: Long-task context and CPU monitoring

**Accepted per-task compaction/restart requirement; settings and engine support open, 2026-10-08.** Before unattended admission, give each long-running model task a numeric context ceiling and supported automatic compaction threshold or verified checkpoint/restart policy. CPU observers retain logs/state and send bounded changed evidence. Wake a model only for reasoning, an actionable exception or a decision; elapsed time alone must not trigger repeated context-heavy polling.

Checkpoint exact source, worktree, effects, jobs, cursors, next action and remaining authority/usage. Revalidate controls and billing after restart. Reattach by verified job ID; never relaunch an existing job, erase uncertain effects, clear a stop, reset usage or renew approval. Bound restart costs and park unchanged loops. Qualify native compaction if no `restart-me` skill exists. Architecture sections 9 and 14.

<a id="adr-0018"></a>
## ADR 0018: Drain workers before approval limits

**Accepted proactive drain; margins open, 2026-10-08.** Reduce new admission before an approval deadline or usage cap. Let draining workers finish only work still authorized and retire from that approval scope. No new task enters a drained scope, even if individually approval-exempt. Draining cannot expand budgets or deadlines.

Use conservative task-duration and whole-task usage estimates, including validation, integration, correction and checkpoint time. Stale estimates favor earlier drain. Keep reservations and uncertain exposure; enforce cached limits offline. Switching account/engine, retrying, quota reset or restarting cannot evade drain. A residual task at expiry follows [ADR 0019](#adr-0019). Website drain margins remain configurable without a selected default. Architecture section 8.

<a id="adr-0019"></a>
## ADR 0019: Expiry handoff and model-query cutoff

**Accepted; five-minute configurable grace default; adapter bounds open, 2026-10-08.** At approval expiry, stop productive model work, compact context, commit only owned WIP and attach a task-linked resume summary. Grace starts at expiry and permits closeout only; reconnect or restart cannot reset it. At grace end, fence model queries from every managed path, including nested calls and auto-resume. Hard usage limits, billing rules and stops still apply.

Record exact task/source/commit/worktree, completed and pending work, tests, jobs, artifacts, next action, authority and exposure. Surface useful owner questions in the task panel and persist answers; answers do not renew approval. Before API cutover, attach a task-linked Markdown handoff; later use task artifacts/history. Record prepared, committed, pushed and attached states separately. Preserve patches and pending attachment/push evidence on failure; do not stage unrelated edits or secrets. [ADR 0026](integration.md#adr-0026) makes task-owned pushes normal delivery, not merges or promotion.

Independently authorized CPU jobs continue under their own bounds and report through API/Cord/observer without model watching. Unknown model activity remains visible. A deterministic retained-progress summary can survive unavailable inference. Architecture sections 5, 8–9 and 14.

<a id="adr-0024"></a>
## ADR 0024: Owner and worker credentials

**Accepted split; issuance/session details proposed, 2026-10-08.** Give owner/admin controlled enrollment, permissions and later approval/budget operations. Give each worker a separate project-scoped credential for required task/Cord operations. Workers cannot mint credentials, expand scopes, approve spending or raise budgets. Tailscale membership grants no application privilege.

Reuse stable principals, hashed token verifiers and server-derived actor identity. Store bearer secrets outside Git and task evidence. Rotation/revocation preserves principal and history; revocation rejects future API calls but does not prove running work stopped. Keep provider and Tailscale credentials distinct. Define the operation matrix, owner recovery and browser sessions before adoption. Architecture sections 3, 5 and 8–9.
