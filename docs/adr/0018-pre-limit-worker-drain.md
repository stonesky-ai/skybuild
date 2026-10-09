# ADR 0018: Drain workers before approval limits

Date: 2026-10-08. Status: accepted owner direction for proactive draining; numeric margins and residual-task handling at actual time expiry remain open. Implementation: not started.

## Context and decision

The owner wants workers drained as an approval limit approaches so very few tasks remain half-finished. Minimize wasted work by preparing before the limit rather than relying on interruption after it is reached.

Drain before approaching approval end time or approved-usage exhaustion. Reduce new admission early, and have draining workers complete existing authorized work and retire from that approval scope. Draining cannot enlarge a budget, renew approval or authorize work past an unapproved deadline. The exceptional task still active at actual time expiry needs a separate finish/grace-versus-checkpoint decision.

## Consequences and proposed mechanics

Use conservative remaining-duration and whole-task usage estimates with validation, integration, correction and checkpoint margins. Preserve existing reservations and uncertain exposure. Unknown/stale estimates favor earlier drain and checkpoint preparation. Make margins configurable in the website; no numeric default is selected here.

Once a scope drains, it admits no new tasks, including tasks exempt from individual approval prompts. Cache relevant deadlines, limits and policy for offline enforcement. CPU observation evaluates progress and headroom; repeated model polling is unnecessary.

Retain resumable checkpoints, completed artifacts, claims and uncertain exposure for residual work. Draining does not prove physical exit or release ownership. Account/engine switching, retries, quota reset and session restart cannot evade drain. A separately authorized window change retains budget history and cannot clear owner stops.

## Alternatives

Waiting until a limit is reached leaves more interrupted tasks. An unconditional finish policy could exceed approved capacity or time. Proactive drain reduces both problems without promising that every estimate or task completion time is exact.

Architecture: section 8. Implementation implications: existing admission/worker drain state, conservative finish estimates, website margins, local deadline enforcement and admission/drain/partition/reset recovery checks in area 5. This refines [ADR 0009](0009-timed-approval-and-daily-provider-caps.md); it does not change the contact-loss default in [ADR 0007](0007-outage-policy-and-deferred-failover.md).

Later decision, 2026-10-08: [ADR 0019](0019-expiry-handoffs-and-model-cutoff.md) resolves the residual task: compact/commit/attach a resume handoff, with short configurable grace for closeout only and then a model-query cutoff. Independently authorized CPU jobs continue and report results without model watching.
