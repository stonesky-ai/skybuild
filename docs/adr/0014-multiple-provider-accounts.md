# ADR 0014: Multiple provider accounts and account rotation

Date: 2026-10-08. Status: accepted owner direction for multiple accounts per provider and rotation near 98% usage; credential implementation and reserve composition remain proposed. Implementation: not started.

## Context and decision

The owner may have multiple Claude, Codex or Grok accounts. Configure them separately and ordinarily use one until about 98% of its relevant quota is used, then switch to another. Make account order and the high-water mark configurable. Central usage must identify actual account/model/shared pool, not only provider.

This allows same-provider account rotation as well as the configurable Codex/Claude coordinator from [ADR 0013](0013-configurable-coordinator-engine.md). Automatic cross-engine failover remains a separate unanswered policy.

## Proposed mechanics and consequences

Keep isolated named credential profiles and immutable account/billing identity per attempt. Store references and non-secret lifecycle metadata in the registry; protect actual credentials in engine profiles/secret storage. Use supported remote enrollment or documented trusted-runner transfer. Credential expiry/refresh is different from allowance expiry/reset; serialize refresh and preserve updated credential versions. A token that permits inference need not permit usage queries. Do not overwrite a global login under active workers or silently fall back to pay-as-you-go credentials.

Bind quota/account reservations and task history across switching. Multiple credentials for one underlying pool do not create independent allowances. Retain daily/interval/provider parent caps and the approval's baseline; enrolling an account does not enlarge an existing budget. Switch earlier if the next bounded task/call cannot fit. Delayed usage evidence cannot guarantee a mathematically exact 98% stop.

The interaction with the 90–95% urgent reserve still needs review. Proposed composition applies the reserve to a configured provider/model pool and the 98% high-water mark to its member accounts. A weighted pool percentage requires known comparable capacities/units; otherwise expose native limits separately. Expiry exceptions remain explicit policy.

Rotate at a supported checkpoint/request boundary, fence and reconcile the old session, and preserve operation IDs, artifacts and uncertain effects. Quota reset or token refresh cannot renew build approval. No eligible account means parked inference, not repeated probing/respawn.

Architecture: sections 8 and 14. Implementation implications: multi-account registry, isolated credential enrollment/refresh, central account-aware readings/reservations and rotation/identity/refresh/old-session-resume checks. [Auth research](../design/research/provider_usage_metering_20261008.md#remote-authentication-and-credential-lifetimes) records documented options and unverified integration details.

Later decision, 2026-10-08: [ADR 0017](0017-pooled-reserve-and-account-rotation.md) accepts the reserve across comparable accounts together, resolving the composition question above. Exact taper/stop rules and expiry exceptions remain open. [ADR 0015](0015-subscription-only-billing.md) records the owner's subscription-only billing restriction on all rotation and remote authentication.

Subsequent decision, 2026-10-08: [ADR 0022](0022-automatic-coordinator-failover.md) resolves the cross-engine question above in favor of automatic qualified Codex/Claude failover within existing authority, budgets and billing policy.
