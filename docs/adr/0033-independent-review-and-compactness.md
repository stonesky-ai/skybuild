# ADR 0033: Independent review and deferred complexity gates

Date: 2026-10-08. Status: accepted owner direction for independent adversarial review, actionable rework and later configurable quality criteria; metric defaults and mechanics proposed. The owner explicitly deferred dedicated complexity/size gates until the self-building parallel-worker MVP is running. Governing architecture: sections 3, 13 and 16, revision A33.

## Context and decision

The owner accepted a separate model-session review for every code task and requested adversarial perspectives that can return work for fixes or targeted tests. Findings must identify affected code, concepts or test cases. The same qualified provider/model can author and review through separate sessions; the author cannot approve its own patch. Existing applicable checks precede the bounded model review. Reuse task attempts, immutable findings/dispositions and the established rework workflow.

Retain GUI-configurable complexity, code size and anti-bloat criteria, but implement them after MVP under SKYBUILD-QUALITY-GATES. Proposed Python defaults use Ruff McCabe complexity 10 and a size warning above 50 statements per function, with source/logical line deltas and evidence-based compactness review. These are documented tool defaults and design proposals, not universal standards. Preserve necessary behavior, safeguards and meaningful tests. Candidate code cannot weaken its own trusted policy or erase findings.

The MVP is a running product that rebuilds and extends itself with parallel workers. The launch-free task/Cord service is an earlier milestone. SKYBUILD-SELF-BUILD-MVP composes minimum controls, a qualified author/reviewer path, workers and bundled integration while the accepted controller remains usable. It does not wait for all legacy extraction, provider variants, advanced cloud features or the dedicated quality-gate suite. Controlled promotion and existing authority/budget boundaries still apply.

## Alternatives and consequences

Self-approval saves tokens but misses independent challenge. Requiring multiple models or a permanent review panel adds coordination and is unnecessary initially. Raw line-count targets invite metric gaming; concrete findings and contract-preserving simplification are more useful. Immediate rollout of all metric gates would delay the owner's intended MVP. Baselines and later scoped exceptions prevent unrelated inherited debt from forcing broad refactors.

Corrections retain task identity, exact input/policy evidence and consumed budgets. Stale passes, reviewer-authored changes, missing reviewer capacity and unresolved blocking findings cannot advance integration. Repeated disagreement or failure parks with an actionable owner question rather than unbounded reviewer retries. Multi-person approval chains remain separately deferred.

## Delivery and evidence

The core review path belongs to implementation-plan area 5a and SKYBUILD-BUNDLED-INTEGRATION. [Review policy](../design/review_policy.md) preserves the deferred criteria, primary-source references and an authored compact prompt for later qualification. The task workbench reuses findings and rework actions; dedicated metric settings arrive with the deferred feature. No runtime implementation, lint installation, worker launch or model review was performed by accepting this design.
