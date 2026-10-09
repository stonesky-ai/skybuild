# Inference execution decisions

Each section retains its original ADR ID. Status applies to the decision, not implementation. [Architecture](../design/architecture.md) governs current requirements.

<a id="adr-0005"></a>
## ADR 0005: Compact CPU-first builder

**Owner goals accepted; components and routing proposed, 2026-10-08.** Start with one API/PostgreSQL service and small shared modules/client. Use deterministic CPU work for discovery, validation, integration, observation and recovery. Later add Keeper and an independent observer per worker box. Keep ownership, authentication and transaction boundaries explicit even when code is compact.

Route bounded tasks by demonstrated capability: CPU first, qualified shared inference when suitable, approved qualified rented GPU for ready backlog, and frontier reasoning for difficult or consequential work. Start with patch/structured output, not a general agent framework. Measure accepted throughput, correction/review effort and shared endpoint limits. Do not silently retry or escalate paid calls. Transient workers drain under explicit budgets/deadlines; durable controller authority stays independent. Candidate self-builds cannot promote themselves. Host location follows [ADR 0006](hosting-recovery.md#adr-0006). Model qualification and numeric budgets remain open. Architecture sections 13–17.

<a id="adr-0013"></a>
## ADR 0013: Configurable coordinator engine

**Accepted Codex/Claude choice; handoff mechanics proposed, 2026-10-08.** Keep deterministic control and durable task state vendor-independent. Select engine/model/account profile through configuration and website. Use a common bounded task, context, tool and artifact contract with small supported adapters, not opaque transcript portability or an always-on premium coordinator.

Checkpoint source/worktree, effects, artifacts, jobs and remaining authority. Fence/reconcile the old coordinator before another starts; preserve operation IDs and uncertain exposure. Charge actual provider pools without resetting history or inventing quota conversions. Park inference if no profile qualifies; authorized CPU/status work remains available. [ADR 0022](#adr-0022) accepts automatic qualified failover. Architecture sections 8 and 14.

<a id="adr-0015"></a>
## ADR 0015: Subscription-only billing

**Accepted restriction; provider enforcement qualification open, 2026-10-08.** Use existing subscription allowances and approved model/rate profiles for all frontier coordinators, workers, children, retries and reviews. Account/engine/host changes grant no API-key fallback, purchased-credit overage, automatic top-up or higher-rate continuation. A future paid mode needs a separate owner decision and monetary budget; approved infrastructure rental is distinct.

Before unattended use, verify account identity, auth source, billing mode, tier and enforceable prevention of paid continuation, including after refresh/switch. Subscription login alone is insufficient if overage is possible. Isolate inherited API credentials and alternate endpoints; do not bypass supported engines with raw APIs. Unknown protection parks affected inference. Cached usage reads make no provider call; unknown query billing cannot justify polling. Architecture sections 8 and 14.

<a id="adr-0022"></a>
## ADR 0022: Automatic qualified coordinator failover

**Accepted automatic Codex/Claude switching; adapters/ownership open, 2026-10-08.** On capacity exhaustion, choose a permitted qualified subscription profile with fresh capacity under the existing approval, budgets, billing rules and native limits. Make primary, alternatives and priority configurable. Enrollment alone does not add a profile to a current approval.

Checkpoint exact work and held exposure; fence/reconcile the old engine before productive replacement. Park if ownership or effects remain uncertain. An old process cannot regain authority after quota reset. Bound switch attempts and overhead. Do not use hopping to evade code/test failures, expiry closeout or model cutoff. Preserve task/provider/day/window accounting. If no target qualifies, park inference while authorized CPU/status work continues. Architecture sections 8 and 14.
