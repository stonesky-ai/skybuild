# ADR 0030: Effect boundaries and accounting refinements

Date: 2026-10-08. Status: proposed mechanics from the A30 architecture audit; existing owner policies remain authoritative. No implementation or deployment is authorized.

## Context and proposed decisions

A29 had strong control requirements but left several transitions between database state, worker execution and external publication implicit. The audit adds the following refinements to architecture sections 2, 7–8 and 13 without adding services:

| Finding | Refinement |
| --- | --- |
| “Strongest” gate profile assumes all profiles are comparable. | Use the union of required checks; deduplicate only equivalent checks or an explicitly established coverage relationship. |
| A lease and pre-merge branch read cannot guard a later GitHub write. | Trusted publisher, durable unresolved publication intent, current dispatch ownership and remote enforcement of the tested head/base relationship. |
| Candidate code could alter its checks or inherit credentials that assert success. | Freeze accepted policy; keep publication and accepted-check credentials outside candidate execution. |
| Parent/child/bundle allocations can double-count charges or abandon uncertain exposure. | Subdivide reservations, settle atomically, allocate a shared model operation once across tasks and retain whole-task history/caps. Return only demonstrably unused allocation when work parks. |
| Cached offline balances and paused clocks can extend authority. | Exclusive per-worker/task/window allocation and suspend-aware deadlines or parked resume pending reconciliation. |
| Restored numeric fences can match still-running old workers. | New recovery authority epoch, quarantined restored authority and reconciliation of surviving external effects. |

One small effect record supplies identity, authority/input/policy references, allocation, dispatch state and external result reference. Store commits intent before external I/O; adapters reconcile outcomes. This is not a distributed transaction or generic workflow engine. CPU controls, one qualified model profile and additional profiles can be delivered incrementally. Unsupported providers cannot disable unrelated CPU work.

## GitHub evidence and limits

GitHub documents the merge `sha` parameter as an expected PR **head**. It does not provide an expected-base parameter on that operation. Asynchronous acceptance or queue enrollment is not merge completion, and an expired result lookup does not prove failure. These facts require an explicit reconciliation contract. [Pull request REST API](https://docs.github.com/en/rest/pulls/pulls#merge-a-pull-request)

Strict required checks require an up-to-date branch; loose checks do not. GitHub also supports restricting a required check to an expected app source. These are candidate building blocks, not evidence that this repository currently has suitable rules or permissions. Qualify the actual plan, rules, bypass rights and merge behavior before automatic publication. [Protected branches](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)

Do not invent a remote atomic compare-and-swap guarantee from a local check. A qualifying PR flow must reject changed tested inputs at the actual publication boundary. A dispatched request can still complete after local owner replacement or stop; retain the slot and reconcile. Runtime deployment/promotion remains separately controlled.

## Design evidence

The [publication model](../design/models/Publication.md) separates gate, dispatch, external target changes, remote result and reconciliation. SANY passed. TLC simulation checked 10,000 traces of depth 40, reporting 400,001 sampled states without a violation. Three deliberate mutations produced the expected counterexamples: missing remote base protection, stale dispatch generation and premature release of an unknown request.

This is bounded sampled design evidence, not exhaustive verification, a liveness result or a test of GitHub. The implementation must qualify the modeled atomic boundaries independently. Earlier admission-model limitations still apply.

## Consequences and remaining choices

Implementation-plan areas 5 and 5a add the corresponding acceptance cases; the low-priority recovery task owns restore-epoch validation. No additional broker, scheduler, policy service or provider account is introduced. Exact GitHub mechanism, check identity, shared model-cost allocation, offline bounds and adapter enforcement remain to be selected. Owner-confirmed scope, allowance limits, automatic bundling and deferred recovery priority are unchanged.
