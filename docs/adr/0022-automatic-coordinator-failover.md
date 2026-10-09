# ADR 0022: Automatic failover between qualified coordinator profiles

Date: 2026-10-08. Status: accepted owner direction for automatic Codex/Claude coordinator failover; adapter/ownership enforcement remains to be qualified. Implementation: not started.

## Context and decision

The owner confirmed automatic switching between Codex and Claude when another qualified subscription profile has capacity, within existing approval, budgets and billing restrictions. This resolves the automatic-versus-manual policy left open in [ADR 0013](0013-configurable-coordinator-engine.md).

On coordinator capacity exhaustion, deterministically select an eligible permitted Codex/Claude subscription profile. Keep the primary engine, allowed alternatives and priority configurable. No additional task-approval prompt is needed for a switch within existing authority. Qualification, fresh capacity and native limits still bind; enrollment alone does not add a profile to an existing approval.

## Consequences and proposed mechanics

Checkpoint exact task/source/worktree, completed effects/artifacts/tests, live jobs, remaining authority and held exposure. Fence and stop/reconcile the previous coordinator before productive replacement; do not assume another CLI can resume its opaque transcript. If exclusive ownership or uncertain effects cannot be reconciled, park the handoff. An old engine resuming after quota reset cannot regain authority.

Preserve task/interval/provider/day budget history and native target limits. Switching cannot renew approval, increase its cap/baseline, enable API/usage-credit billing or select an unapproved model/rate profile. During closeout grace only authorized closeout is eligible; a switch cannot resume implementation after expiry or evade the model-query cutoff.

Bound switch attempts and checkpoint/restart overhead within the original reservation. No eligible target means parked inference, while authorized CPU jobs and status APIs remain usable. Other failures retain their failure circuit; do not repeatedly hop engines to evade code/test failures or unavailable capacity. Use central cached usage and deterministic policy, not model requests to choose a provider.

## Alternatives and validation

Manual-only switching was not selected. Unbounded fallback would risk duplicate work, billing changes and exhausted budgets. Validate capacity exhaustion, stale/unknown target usage, revoked billing identity, no target, uncertain old requests, old-session resume, repeated switching and expiry/cutoff boundaries.

Architecture: sections 8 and 14. Implementation implications: existing Runner/Keeper adapters, portable checkpoints, scoped admission and bounded automatic switching in area 5. This does not authorize any engine session or account access during planning.
