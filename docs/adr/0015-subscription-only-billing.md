# ADR 0015: Preserve subscription billing across execution and switching

Date: 2026-10-08. Status: accepted owner restriction; provider/engine enforcement remains to be qualified. Implementation: not started.

## Context and decision

The owner permits supported remote authentication and multiple-account use only while retaining ordinary existing subscription costs. The owner explicitly rejects API rates and usage-credit pricing. Account rotation near 98%, engine choice and an eight-hour approval do not grant permission for paid continuation.

Frontier inference uses existing subscription allowance and approved model/rate profiles. No automatic API-key fallback, purchased usage-credit overage, top-up or higher-rate option is allowed. Apply the restriction to the coordinator, workers, children, retries, reviews and every execution host. Any future paid mode requires a new explicit owner decision and a separate monetary budget. Separately approved infrastructure/GPU rental budgets remain distinct.

## Consequences and proposed enforcement

Qualify actual account identity, authentication source, billing mode, model/tier and prevention of paid continuation before unattended adoption. Subscription login alone does not establish protection: an account may allow paid overage after a native limit. Isolate launch profiles from inherited API credentials, helpers and alternate billing endpoints; recheck after refresh, account/engine switching and relevant configuration changes. Do not call raw model APIs with subscription credentials to bypass the supported engine.

Unknown or unsupported protection parks affected inference and exposes the reason. Exhaustion selects another qualified subscription profile within existing budgets or waits. It never buys capacity or changes billing mode. No extra usage-query fees are authorized; unknown query billing cannot enable active polling. SkyBuild's cache-only usage GET never makes a provider call.

Qualify and verify exhaustion, conflicting credentials, paid-overage settings, remote enrollment, rate/tier changes, child calls and old-session resume. No credentials were inspected or transferred, account settings changed or authenticated billing/inference requests made during planning.

## Alternatives

Using API credentials for easier automation was rejected by the owner. Assuming all subscription-authenticated requests are covered by included allowance is insufficient. A new universal token broker would not establish billing protection and is unnecessary initially; retain small qualified provider/engine adapters.

Architecture: sections 8 and 14. Implementation implications: billing eligibility in existing admission and engine profiles, provider qualification and zero-paid-continuation acceptance cases. This qualifies the account rotation in [ADR 0014](0014-multiple-provider-accounts.md) and engine selection in [ADR 0013](0013-configurable-coordinator-engine.md). [Billing research](../design/research/provider_usage_metering_20261008.md#subscription-billing-boundary) records supporting public documentation and integration unknowns.
