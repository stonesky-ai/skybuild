# ADR 0034: Post-MVP quality debt and serialized repository review

Date: 2026-10-08. Status: accepted owner direction for post-MVP priority, thresholds and serialization; counting, fencing and GUI mechanics are proposed. Governing architecture: sections 3, 13 and 16, revision A34.

## Context and decision

ADR 0033 deferred dedicated complexity and size checks until the self-building parallel-worker MVP runs. The owner now requires pinned Ruff checks, including McCabe complexity, in the subsequent code-review path. Some of the first post-MVP tasks must clear all actual tech debt those checks expose across the repository. A legacy baseline may stage enforcement but cannot silently waive real findings indefinitely. Corrections preserve required behavior and useful tests and pass the ordinary independent review and publication gates. Verified false positives or findings that are not debt require a narrow authorized disposition.

Additional whole-repository code review runs as a background task whenever either 1,000 accepted commits or 5,000 changed source-code lines accumulate after the last completed review, whichever occurs first. The owner requires serialization: no two whole-repository reviews run together, and the next review cannot start until fixes for every actionable finding from the previous review have been accepted and published. An accidentally repeated review must not duplicate findings or fix tasks. The two numeric thresholds become GUI-configurable later.

Proposed mechanics count accepted source additions plus deletions once across branch and merge boundaries, excluding documentation and generated files. Persist the completed baseline, counters, pending trigger, reviewed source revision, run identity and findings. Acquire one fenced review slot before dispatch; lost acknowledgments and unknown outcomes retain the slot until reconciled. Threshold crossings while the slot is held remain pending and coalesce. Deduplicate repeated work by review identity, baseline and source revision, and preserve finding identity across retries. CPU scheduling never bypasses qualified model review, budgets, subscription-only billing or cutoff. Exact counting commands, rule selection and GUI interaction need implementation qualification.

## Consequences

The MVP still uses existing applicable checks and independent adversarial review; this richer pipeline does not become a retroactive MVP prerequisite. SKYBUILD-QUALITY-GATES and SKYBUILD-QUALITY-DEBT-CLEANUP become early post-MVP tasks. The recurring review starts after initial debt is cleared. The hold may grow a visible backlog when fixes or model capacity are unavailable; it must not launch overlapping scans to catch up. Task history retains original findings, fixes, explicit dispositions and accepted publication evidence.

The alternative of overlapping whole-repository reviews was rejected because a later scan can rediscover unfixed findings and create duplicate or conflicting work. A permanent baseline waiver was rejected because it would not satisfy the owner's cleanup direction. A numeric calendar ETA for MVP or cleanup is not selected here.
