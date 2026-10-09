# ADR 0009: Time-limited approval and daily provider usage caps

Date: 2026-10-08. Status: accepted owner direction for time-limited approval and aggregate daily build-usage caps per frontier model provider and Claude's 10%-of-weekly daily default, eight-hour configurable approval and an overall interval cap; accounting and expiry mechanics remain proposed. Implementation: not started.

## Context and decision

The owner prefers approving execution for a period of time over approving each larger task. Overall build usage must remain below a configured daily percentage for each frontier model provider. The control website manages the approval window and separate provider caps. Eligible larger tasks proceed without individual task approval during that window. Approval expires rather than renewing itself. The owner subsequently selected eight hours as the configurable default, with explicit end times also supported. Each approved interval also has an overall usage cap, for example no more than 12% of a weekly allowance between now and noon tomorrow; this is an example, not a selected default cap.

The configurable below-1% task exemption from [ADR 0008](0008-task-allowance-and-approval-threshold.md) remains. Exempt tasks still consume shared daily capacity and obey native provider limits. The full weekly Claude allowance remains the task-estimate basis; shorter usage limits are separate checks. This decision resolves the larger-task approval mode left open in ADR 0008. It does not authorize execution in the current planning session.

## Proposed mechanics and consequences

Bind each approval to start/end, project/task scope, permitted providers and a policy version. Daily usage covers all managed build work across projects, boxes, tasks, child calls, review, integration and retries. A new approval or renewal cannot reset daily consumption. Explicit provider/account budget pools prevent separate workers or linked accounts from multiplying the cap.

Reserve task, provider/day, approval-window and native-account capacity atomically. Retain uncertain in-flight exposure across disconnection, day rollover and reset; reconcile actual usage without double counting. Switching providers requires eligibility under the destination's approval and cap. Show usage source/freshness, estimate confidence and remaining headroom; an unreliable meter cannot establish an exact universal quota ceiling.

The owner subsequently confirmed Claude's default daily build cap at 10% of its full weekly allowance, adjustable in the control website. Other providers need an explicit allowance baseline and native accounting; percentages are not interchangeable. Other-provider numeric caps, interval-cap defaults/account-model-pool mappings, accounting-day/timezone, estimation method and active-task behavior at approval expiry remain open. Expiry fences new task admission under that approval window. Offline completion can consume only already-reserved capacity and cannot enlarge the daily budget.

Alternative: approve each task or batch. The owner selected time-limited approval instead, with aggregate daily usage controlling consumption.

Architecture: sections 8 and 12. Implementation implications: window scope/expiry, website provider caps, aggregate reservations and evidence for renewal, rollover, provider switching and offline reconciliation.

Later decision, 2026-10-08: [ADR 0018](0018-pre-limit-worker-drain.md) accepts proactive worker draining as approval time or usage limits approach. Numeric drain margins and the exceptional task still active at actual time expiry remain open; this is not an automatic extension of approval.

Subsequent decision, 2026-10-08: [ADR 0019](0019-expiry-handoffs-and-model-cutoff.md) resolves residual-task handling with compact/commit/handoff and a short configurable closeout-only grace, followed by a model-query cutoff. CPU jobs retain their independent lifecycle and reporting. Numeric settings and qualified enforcement remain open.
