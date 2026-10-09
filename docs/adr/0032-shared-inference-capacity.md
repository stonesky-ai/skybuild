# ADR 0032: Shared zero-charge inference as build capacity

Date: 2026-10-08. Status: accepted owner direction to include the friend's models, bounded coding and project-visible tradeoffs; pool limits are owner reports, qualification and scheduling mechanics proposed. Governing architecture: sections 8 and 13–15.

## Context and decision

The owner offers a friend's free server with four `qwen3.5-think` slots, two `recall-honcho-8b` slots and one `bge-m3` slot. Include this in initial controlled inference capability, with tasks routed there to conserve frontier allowance. Limited Qwen coding is a qualification candidate. The other pools support fact extraction and retrieval; they do not add three coding workers. Model aliases do not prove weight identity, quality or sustainable combined concurrency.

Project creation exposes this profile, permitted data scope, allowed work, fallback preference and quality/latency/review tradeoffs. Reuse one shared resource ledger across projects, coding and product tests. Use useful available capacity while protecting integration-critical work; reduce drafting when validation/review becomes the bottleneck. Do not silently change product-test model requirements or assume another client's requests are covered by SkyBuild's reservation.

The owner subsequently specified installation-local `.env` configuration and generic option names. Use `SKYBUILD_INFERENCE_BASE_URL` and `SKYBUILD_INFERENCE_SECRET_FILE`, the latter holding only a path to the external bearer-token file. Ignore local `.env` files; share an empty `.env.example`. The current host and credential filename are values, not built-in option names. This clarification is reflected in architecture A32; no runtime loader has been implemented.

This is a zero-charge shared profile, distinct from frontier subscription profiles and paid rentals. Subscription percentages are not applicable to its own requests. Applicable authority, resource, deadline, query-cutoff and correction limits still apply; coordinating/reviewing frontier calls retain their actual budgets. An OpenAI-shaped API is a protocol, not permission to incur OpenAI API charges. Credentials stay profile-specific. The first qualified model slice may use this endpoint; premium account rotation is not its prerequisite.

## Alternatives and consequences

Frontier-only generation would waste offered capacity. Treating all seven slots as coding workers would misrepresent model roles and contention. A full autonomous tool-use or memory platform is unnecessary: begin with bounded patch/structured-output requests and existing controlled tools, source references and validation. Embedding retrieval is incremental and optional where graph/text search is adequate.

Free inference may increase correction cost or integration latency. Measure accepted changes, total frontier usage and time through integration. Qualification must cover relevant task classes, bounded failures and shared-capacity behavior; a toy successful completion does not qualify general coding. Server ownership and operational control remain with the friend: an empty SkyBuild queue releases reservations, not power or model lifecycle controls.

## Evidence and delivery

[Discovery evidence](../design/research/shared_inference_capacity_20261008.md) separates owner reports, local configuration, upstream model cards and live unknowns. The owner authorized capability queries and indicated existing SkyKeep credentials. Initial attempts failed at DNS; they did not retrieve metadata or inference output. An A32 evidence follow-up after network access became available used the generic SkyBuild `.env` settings and external bearer-token file. An authenticated HTTPS `/v1/models` request returned HTTP 200 and aliases `bge-m3`, `qwen3.5-think` and `recall-honcho-8b`, each reporting `owned_by: llamacpp`. This does not verify weights, concurrency or coding quality. No secret was printed or persisted, no inference request was made and no serving configuration was changed.

SKYBUILD-SHARED-INFERENCE is proposed core work, implementation-plan area 5b, following the applicable control slice. Qualification of one pool does not require enabling the others; embedding/fact extraction do not block initial qualified Qwen coding. No runtime implementation or benchmark is authorized by this ADR.
