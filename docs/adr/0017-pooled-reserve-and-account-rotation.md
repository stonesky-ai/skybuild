# ADR 0017: Pool the urgent reserve across comparable accounts

Date: 2026-10-08. Status: accepted owner decision for reserve composition; pool qualification, exact taper/stop rules and expiry exceptions remain open. Implementation: not started.

## Context and decision

The owner wants to preserve urgent capacity near 90–95% total allowance consumed and ordinarily rotate an individual account near 98%. Applying both marks separately to each account would usually stop routine work before its rotation mark.

Apply the urgent reserve across comparable accounts together in a configured provider/model allowance pool. An individual account can reach approximately 98% while the combined pool retains spare capacity. Include observable non-build usage in the reserve basis, as previously confirmed. Individual native limits and all existing task, daily and interval caps remain binding.

## Consequences and proposed mechanics

Qualify actual pool membership, comparable allowance units/windows, known capacity and accessible accounts. Aggregate with capacity weights, deduplicating credentials/models sharing one native bucket. Do not average unlike percentages, combine unrelated providers or count unknown/inaccessible capacity as spare. Combined headroom cannot override an exhausted account-native limit.

Account rotation does not replenish a pool, renew approval or multiply its build cap. Adding an account cannot silently enlarge an existing approval's baseline or budget. Retain reservations and uncertain exposure across switching. Subscription-only billing remains required.

Exact taper/park behavior and access to the reserve remain open, as does any exception for useful consumption of soon-expiring capacity. This decision does not select new numeric thresholds, authorize paid overage or enable execution.

## Alternatives

Preserving the urgent reserve independently in every account was not selected. Treating all providers or unknown plan sizes as one interchangeable percentage balance is invalid.

Architecture: section 8. Implementation implications: capacity-aware pool readings/reservations, account-level native checks and tests covering unequal capacities, shared buckets, non-build consumption and membership changes. This resolves the reserve-composition question in [ADR 0014](0014-multiple-provider-accounts.md) and refines the reserve scope in [ADR 0010](0010-frontier-capacity-reserve.md).
