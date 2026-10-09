# ADR 0011: Central cached usage readings

Date: 2026-10-08. Status: accepted owner direction for a central percentage-used REST endpoint and model-level visibility; concrete route, adapters and refresh policy remain proposed. Implementation: not started.

## Context and decision

Components must share usage readings rather than independently asking providers. The owner also wants readings by model so allocation can account for available and expiring capacity.

Expose one authenticated REST usage surface, proposed as `GET /api/v1/usage`, with provider/account/model/budget-pool filters. Reads return cached metadata and never trigger provider refresh or inference, even when the cache is missing. The website, CLI, workers and admission share this source. The response is not a launch permit: atomic admission still reserves remaining capacity.

## Proposed mechanics and consequences

Use one logical collector in the existing API/Store with small provider adapters, fenced ownership, coalesced bounded refresh and backoff. Prefer passive engine metadata and documented status APIs. Verify adapter support, authentication, billing and freshness before enabling it. Do not ask a model for usage or assume a CLI slash command is a headless API.

Expose provider/account/model and actual shared quota-pool identity, native windows, percentage/remaining capacity when known, reset/expiry time, source/coverage and freshness/status. A provider may meter several accounts or models together; those models reference one pool rather than each receiving its full capacity independently. Model attribution is unknown when unsupported. Unknown percentage is null, not zero. Account-wide quota and attributed build usage remain separate.

Components cannot invent authoritative readings or obtain provider credentials through the response. Unsupported/stale sources remain visible and block affected new admission. Read-only caching uses CPU/HTTP, not model tokens; provider polling fees are not universally verified. [Metering research](../design/research/provider_usage_metering_20261008.md) records confirmed surfaces and limits without live account queries.

Architecture: sections 8 and 13–14. Implementation implications: shared cached REST, collector ownership/adapters, source qualification and checks that concurrent readers do not multiply provider queries or spend inference tokens.
