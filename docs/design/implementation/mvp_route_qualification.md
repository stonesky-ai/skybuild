# MVP author and reviewer route qualification

Task: `SKYBUILD-MVP-ROUTE-QUALIFICATION`. This brief implements the offline procedure and evidence format. It does not select a profile, authorize inference, or qualify a route.


Source contract: architecture revision A41 at base `b863f17520683066fe7bfd2e37f1b57e31736945`, SHA-256 `353a2a575311d15f849f82022000586518603cce3d3f968b4fd024229568e823`; sections 8–9 and 13–16 govern. Source preparation: `93ea9914b15016817be2aa886dfbcba537324752`; task definition: `8e44b41a7d354aea25deec20d6d0eed3a5d41c24`. This bounded port preserves the stable task identity without restoring the retired Markdown ledgers or importing unrelated Workbench implementation. The authenticated task API owns current task state; registration and current task revision must be verified there before any managed evaluation.

## Mandate

Qualify one permitted route for bounded SkyBuild feature authoring and a separate independent reviewer session. Authoring and review are distinct capabilities. Accept a route only for the task classes supported by its evidence. Keep unsupported classes unavailable.

The route identity is the complete execution contract, not a model name alone: provider/profile, exact model/build identity, request template and settings, adapter/runner revision, context construction, output limits, validation path, and applicable billing/authorization controls. Any change to these inputs invalidates affected evidence.

## Current evidence and reuse

The [Brodson canary record](../../research/brodson-spike/qualification.md) provides reusable offline fixtures, bounded CPU validators, response accounting cases, and a clear separation between proposal, validation, and review. It does not qualify an MVP author/reviewer route.

The [four-concurrent Brodson report](https://github.com/stonesky-ai/skybuild/blob/8fd5d21c455b05557e4213dffced6169617a2563/docs/research/brodson-spike/four-concurrent-20261009.md) records 3 accepted answers from 12 technical generations. Code repair, bug review, and test design were not accepted. Only a small rule-based classification case passed consistently; deterministic CPU processing is preferable for that case. Do not admit the tested Qwen profile for MVP code authoring, review, or test design on this evidence.

The serial canary's synthetic review case and the concurrent probes' repeated prompts are preparation/evidence artifacts only. They do not prove review competence, held-out performance, endpoint availability, or current capacity. Existing independent reviews of Brodson implementation commits assess those code changes; they do not qualify Brodson as a reviewer.

No alternative route is selected or qualified by this brief. Route selection, model calls, credential access, and usage require the profile-specific separate authority described in the authoritative API task and project policy.

## Qualification sequence

1. **Freeze scope and route.** Name only bounded feature task classes the route is expected to handle. Exclude deployment, authority changes, secrets, security-sensitive changes, concurrency protocols, broad architecture changes, and integration approval unless separately qualified. Pin all route identity fields and the source revision of the harness/template. A missing identity or unenforceable billing/usage limit blocks evaluation.
2. **Prepare independent cases.** Select at least three distinct, representative SkyBuild feature tasks with explicit definitions of done and deterministic checks. Include normal behavior, boundary/error behavior, and at least one regression-sensitive case. Prepare expected behavior and checks before inference. In addition, prepare at least three held-out review-only code cases, including seeded blocking defects and clean controls. Keep their expected findings out of the review prompt and author session.
3. **Evaluate authoring.** For each task, run a bounded author session on a pinned base. Record exact input, prompt/template digests, output, candidate head and diff digest, all returned usage, wall time, validation, and correction count. Allow at most one corrective author response per case within the separately authorized per-case and aggregate budgets. Never execute untrusted output without normal code review and checks.
4. **Evaluate review separately.** Start a distinct reviewer session with no shared conversation state. Give it the exact candidate task contract, source context, full diff, and applicable check results. Bind every review to the exact commit and diff digest. Review each authored feature head and the held-out review-only cases. Score every expected blocking finding and every false positive; do not invent a minimum finding count. A reviewer edit changes authorship and requires a new independent review.
5. **Check actual task evidence.** For each authoring case, require the applicable project checks to pass and a separate reviewer to accept that exact candidate head or return actionable findings. Corrections preserve budget and history; re-run affected checks and exact-head review. An accepted toy fixture cannot substitute for a real representative feature task.
6. **Write immutable result.** Fill the [record template](../evidence/mvp_route_qualification_record.template.json), preserve artifact digests and sanitized evidence references, and compute `record_sha256` over canonical UTF-8 JSON with sorted keys, compact separators, unescaped Unicode, and the `record_sha256` field omitted. Keep each request and correction as a separate attempt, including failed attempts, unknown outcomes and their retained usage exposure. Record aggregate consumed, reserved and uncertain usage in the applicable native units without summing unlike provider percentages. Keep limitations. An independent evaluator checks route identity, source/head/diff bindings, separate author/reviewer/evaluator session identities, usage totals, oracle results, and all required stop conditions. Later corrections create a new record that names the superseded digest; never edit a completed record in place.
7. **Limit the claim.** Record `qualified` only for the exact route revision and task classes that meet every required check. Otherwise record `not-qualified` or `blocked`, with a named reason and next action. Qualification does not grant task dispatch, repository write, merge, deployment, or additional usage authority.

## Required evidence and decision rules

Mark each record case `author-and-review` or `review-only`. A held-out review-only case has `author: null` and binds its fixture source/head/diff directly in `fixture` with its input artifact digest; it must not be generated by the candidate author session. For every author case, retain the task/definition digest, base/head commit, diff digest, author session ID, exact profile/build/template/harness identity, prompt and response artifact digests, requests and returned usage, latency, validation commands/results, correction count, and independent acceptance outcome.

For every reviewer case, retain a reviewer session ID different from the author session ID, the exact reviewed head and diff digest, task/check evidence digests, reviewer output digest, independently prepared expected findings, detected/missed finding dispositions, false positives, and the separate evaluator's result. The evaluator session must also differ from both author and reviewer sessions. The reviewed head and diff digest must exactly equal the submitted candidate. Review evidence for a stale head is invalid.

Aggregate evidence must show actual enforceable billing mode, budget reservation and cutoff behavior, context and response bounds, timeout/unknown-outcome handling, no automatic fallback, and consumed/uncertain usage. A documentation claim or synthetic authority object is not enforcement evidence. Store secrets outside artifacts; retain only safe credential references and sanitized metadata.

Stop and leave route unqualified when profile identity or billing protection is unknown, an authorization expires, budget/usage is missing or ambiguous, a route input changes, a request times out with unknown server outcome, a required independent check is unavailable, a blocking reviewer finding is missed, or corrections exceed the bound. Do not retry through another model/provider to obtain acceptance.

## Definition of done

- A completed immutable record identifies one exact route and a finite list of qualified task classes.
- At least three representative feature tasks have author evidence, applicable passing checks, and separate-session exact-head review.
- At least three held-out, scope-matched review-only cases include seeded blocking defects and clean controls, in addition to exact-head review of authored features; an independent evaluator accounts for every expected blocking finding.
- The record demonstrates applicable authorization, billing/usage enforcement, context/cutoff limits, bounded correction, and failure/unknown-outcome handling.
- An independent evaluator verifies exact profile/template/harness, source, head, diff, session, evidence-digest, and usage bindings.
- Limitations and excluded classes are explicit; no synthetic-only result is called qualification.

Until those conditions are met, qualification remains incomplete. Offline preparation can be integrated without representing the route or the overall task as qualified or done. The current Brodson evidence is a negative qualification result for coding, bug review, and test design, not a blocker to writing this procedure. Completing the live route qualification waits for a suitable route, its applicable control slices, representative work, and separate authority for any inference usage.

## Preparation checkpoint

The owner/dispatcher resolves the next blocker: select a permitted candidate route, establish its applicable execution and billing controls, verify its authoritative API task identity/revision, then obtain any separately required inference authority. No route is qualified, no model call is performed, and no task API mutation or runtime promotion is part of this preparation. Dependencies remain the bootstrap/task API, the applicable execution-control slice and shared-inference controls when that profile is selected. This document and the template are preparation deliverables, not a completed qualification record or a replacement task ledger.
