# ADR 0013: Configurable Codex or Claude coordinator

Date: 2026-10-08. Status: accepted owner direction for coordinator engine choice; automatic failover and handoff mechanics remain proposed. Implementation: not started.

## Context and decision

The model coordinating build processes must itself be configurable to run under Codex or Claude Code. Exhausting one engine's allowance must not tie SkyBuild permanently to that engine or prevent the owner from selecting an eligible alternative.

Keep the deterministic control plane and durable task state independent of agent vendors. Select engine and model/account profile through configuration/control website. Use the same bounded task, context, tools and artifact contract, adapting supported engine permissions/capabilities rather than depending on vendor transcript formats. Reuse Runner/Keeper adapters; do not introduce a universal agent framework or always-on premium coordinator.

## Proposed mechanics and consequences

Checkpoint exact task/source/worktree, completed effects, artifacts/test evidence, unresolved work and remaining authority/budgets. Fence and stop/reconcile the old coordinator before continuing under another engine. Preserve operation IDs and uncertain effects so an old process that resumes after quota reset cannot duplicate the replacement's work. A new engine receives a portable checkpoint, not an assumed native resume of another CLI's conversation.

Each attempted/fallback call uses its actual provider/model quota pool and remains in task history. Qualification, shared parent caps, interval/daily limits and owner stops remain binding. A switch cannot invent a Claude-to-Codex percentage conversion or reset the task's cost history. Automatic failover versus offering a manual switch remains open; any automatic policy must name permitted alternatives and honor their remaining authority/capacity.

If no reasoning engine is eligible, park inference work. The API, usage/status cache and authorized deterministic CPU operations remain usable.

Architecture: sections 8 and 14. Implementation implications: two qualified coordinator adapters, portable checkpoints, control-website engine choice and exhaustion/handoff/old-process-resume checks before automatic adoption.

Later decision, 2026-10-08: [ADR 0022](0022-automatic-coordinator-failover.md) confirms automatic switching to another qualified permitted Codex/Claude subscription profile with available capacity, within existing approval, budgets and billing restrictions. This resolves the policy question above; safe handoff/anti-loop enforcement still requires qualification.
