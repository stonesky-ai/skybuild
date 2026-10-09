# ADR 0012: Prefer useful work on expiring model capacity

Date: 2026-10-08. Status: accepted owner direction for model-aware assignment and a strong preference to consume soon-expiring allowance; routing and reserve exceptions remain proposed. Implementation: not started.

## Context and decision

An owner might have substantial Grok allowance expiring in 24 hours while another model's capacity remains scarce. Usage visibility and assignment must distinguish models and consider expiration. The owner almost always wants soon-expiring capacity used completely.

Prefer qualified, useful tasks on the eligible model/pool whose usable capacity expires soonest. Do not manufacture requests merely to exhaust allowance. This is an allocation goal, not evidence that the example Grok balance exists or that its expiration has been verified.

## Proposed mechanics and consequences

Use the central usage snapshot with provider/model, shared pool, native units, remaining capacity, expiry/reset provenance and confidence. Distinguish a credit's expiration from quota replenishment/reset. Preserve shared parent limits and account for one pool once, even when several models can spend it.

Apply capability, task quality, input/context fit, validation, dependencies, approved window and hard budgets before expiry preference. Then order ready work by real expiry and estimated useful throughput, with configurable owner priorities. Avoid switching models in ways that invalidate product-test evidence. Report capacity projected to expire unused and the blocking reason. More concurrency is useful only within measured endpoint/CPU limits.

The goal is near-complete useful consumption before expiration, not necessarily immediate depletion while urgent work might still need the model. The interaction with the ordinary 90–95% reserve is unresolved. Proposed policy permits a configured expiry exception to relax that reserve for selected pools; it cannot silently increase interval/daily/money caps or extend approval. The owner must settle whether that exception is automatic and when it applies. Keep the dispatcher deterministic and compact rather than adding a separate optimization service.

Architecture: sections 8 and 14. Implementation implications: model/pool-aware reads, expiry-aware deterministic ordering, shared-limit accounting, reserve policy and evidence for unsupported/stale expiry, task eligibility and hard-cap enforcement.
