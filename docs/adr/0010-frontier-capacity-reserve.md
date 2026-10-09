# ADR 0010: Preserve frontier-model capacity through throttling

Date: 2026-10-08. Status: accepted owner goal and total-provider-usage basis; exact thresholds and reserve-access mechanics remain proposed. Implementation: not started.

## Context and decision

Daily build caps do not by themselves ensure that a frontier model remains available for urgent work. The owner wants build activity to slow as usage approaches 90–95%, preserving spare capacity when a task absolutely requires that model.

This adds a reserve goal to the time-window and daily-cap policy in [ADR 0009](0009-timed-approval-and-daily-provider-caps.md). Claude's confirmed default daily build cap remains 10% of its full weekly allowance. The owner subsequently confirmed that the 90–95% range refers to total provider allowance consumed, including non-build work.

## Proposed mechanics and open choices

Use the confirmed total provider allowance basis, including non-build use, with shorter native limits checked separately. Propose tapering new routine work from 90% and parking it at 95%. These exact thresholds and the parking rule are proposals, not accepted owner decisions.

Reuse deterministic admission to reduce concurrency and space bounded requests. Include reserved and uncertain in-flight exposure; do not create token-consuming waiting loops or kill/relaunch calls to throttle them. The website shows the quota basis, freshness, reserve and reason for slowing. Small-task exemptions and active approval windows do not bypass the reserve. Reserve access remains under owner control; its detailed override policy is open.

Architecture: section 8; open decisions in section 12. Implementation implications: reserve-aware admission/provider accounting and checks for threshold crossing, concurrent reservations, stale quota evidence, non-build use, resets and unauthorized reserve bypass.
