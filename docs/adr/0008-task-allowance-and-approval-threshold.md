# ADR 0008: Task allowance percentages and configurable approval threshold

Date: 2026-10-08. Status: accepted owner direction for weekly budget basis, approval exemption and website configuration; accounting mechanics remain proposed. Implementation: not started.

## Context and decision

Premium subscription allowance is scarce, and asking for approval on every small task adds unnecessary interruption. Scope every task as a percentage of Claude's full weekly usage allowance. The owner subsequently confirmed the weekly basis, with shorter usage limits checked separately. Tasks estimated strictly below 1% require no task approval. Expose this threshold in the control website; exactly 1% is outside the default exemption. Budgeted substeps of a qualifying task do not introduce separate task-approval prompts.

This replaces the proposed requirement to approve each selected task manually. The estimation source remains open. The later owner decision in [ADR 0009](0009-timed-approval-and-daily-provider-caps.md) resolves larger-task approval through time-limited windows under daily provider caps. The decision describes future execution policy; the current session remains planning only.

## Proposed mechanics and consequences

Use percentage points of the named account's full weekly allowance, with estimate provenance, confidence, cap, reserved/observed usage and unknown exposure. A changing remaining balance is not the denominator. Do not infer a token-to-quota conversion or reliable provider meter without evidence.

Include the full task lifecycle and bounded correction/retries in its estimate. Children draw from the parent reservation. Shared account/window reservations prevent many exempt tasks from oversubscribing available capacity. Splitting, resume or retry cannot reset cost history. Unknown estimates do not establish eligibility; scope/cost growth requires re-estimation before more model work. Offline exhausted-cap behavior still needs a decision.

The approval exemption removes task-approval prompts; deterministic readiness, stop, ownership and resource checks still apply. Claude percentage is a planning unit, not a conversion into other providers' quota or unlimited cloud money. Track those capacities and limits separately.

Alternative: explicit approval for every selected task. Rejected for tasks below the configured threshold because the owner wants them to proceed without interruption. Larger-task approval was subsequently resolved by ADR 0009; it does not change the small-task exemption.

Architecture: sections 8 and 12. Implementation implications: percentage budgets, website policy/version, whole-task and account/window accounting, honest estimation, and threshold/concurrency/retry/reset acceptance cases before model execution.
