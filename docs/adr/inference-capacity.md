# Inference capacity decisions

Each section retains its original ADR ID. Status applies to the decision, not implementation. [Architecture](../design/architecture.md) governs current requirements.

<a id="adr-0008"></a>
## ADR 0008: Task allowance and approval threshold

**Accepted weekly basis and configurable exemption; accounting proposed, 2026-10-08.** Estimate each task as a percentage of a named account's full weekly Claude allowance. Tasks strictly below the default 1% threshold need no individual task approval; exactly 1% does. Expose the threshold in the website. Substeps of one qualifying task do not add approval prompts. Larger work uses [ADR 0009](#adr-0009).

Track estimate source/confidence, full lifecycle, corrections, reservations, observed use and uncertain exposure. Children draw from the parent. Splits/retries/resumes retain cost history. Shared reservations prevent many exempt tasks from oversubscribing. Unknown estimates do not qualify. Exemption never bypasses stops, ownership, resources, daily caps or billing restrictions. Claude percentages do not convert to another provider's quota or cloud money. Architecture sections 8 and 12.

<a id="adr-0009"></a>
## ADR 0009: Approval windows and daily provider caps

**Accepted eight-hour configurable default, interval cap and daily caps; mechanics proposed, 2026-10-08.** Approve eligible larger work for a bounded interval, with explicit end times supported. Bind approval to scope, providers, policy version and an overall interval usage cap. Set aggregate daily build caps per frontier provider in the website; Claude defaults to 10% of its full weekly allowance per day. A new window cannot reset daily consumption. The below-1% exemption remains, but exempt work consumes shared capacity.

Count all managed build work across projects, workers, children, review, integration and retries. Reserve task, provider/day, window and native-account capacity atomically; retain uncertain in-flight exposure across disconnection and rollover. Check native shorter limits separately and show source/freshness. Provider percentages are not interchangeable. Other-provider baselines/caps, timezone, estimates and pool mapping remain open. Expiry follows [ADR 0019](execution.md#adr-0019). Architecture sections 8 and 12.

<a id="adr-0010"></a>
## ADR 0010: Urgent frontier reserve

**Accepted reserve goal and total-provider-use basis; taper details proposed, 2026-10-08.** Slow routine build work near 90–95% of total provider allowance consumed, including non-build use, to preserve urgent capacity. Tapering at 90% and parking at 95% are proposals, not selected thresholds. Reserve access remains under owner control.

Use admission to limit concurrency and include reserved/uncertain exposure. Do not burn tokens in waiting loops or kill/relaunch calls to throttle. Show basis, freshness, reserve and reason. Small-task exemption and active approvals do not bypass reserve. Apply reserve across comparable accounts per [ADR 0017](#adr-0017); expiry exceptions remain open. Architecture section 8.

<a id="adr-0011"></a>
## ADR 0011: Central cached usage readings

**Accepted central REST surface and model visibility; route/adapters proposed, 2026-10-08.** Serve authenticated cached provider/account/model/pool usage, proposed as `GET /api/v1/usage`. Reads never refresh providers or invoke inference. Website, CLI, workers and admission share one source; a reading is not an admission permit.

Use one bounded collector with qualified provider adapters, coalesced refresh and backoff. Show actual shared quota-pool identity, native windows, remaining capacity where known, reset/expiry, source, coverage and freshness. Do not multiply a shared bucket across models/accounts. Represent unknown percentage as null, not zero; distinguish account quota from build attribution. Stale/unsupported readings stay visible and block affected new inference admission. Do not infer a headless API from a CLI command. Architecture sections 8 and 13–14.

<a id="adr-0012"></a>
## ADR 0012: Useful work on expiring capacity

**Accepted expiry preference; reserve exception proposed, 2026-10-08.** Prefer useful qualified tasks on eligible capacity that expires soonest. Do not generate waste solely to exhaust an allowance. Distinguish credit expiry from quota reset, shared pools from model aliases, and observed capacity from an illustrative example.

First enforce capability, quality, context, validation, dependencies, approval and hard budgets. Then order ready work by actual expiry and useful throughput. Report projected unused capacity and blockers. A configured expiry exception might relax the ordinary reserve, but cannot raise money/daily/interval caps or extend approval; automatic use remains undecided. Architecture sections 8 and 14.

<a id="adr-0014"></a>
## ADR 0014: Multiple accounts and rotation

**Accepted multiple accounts and near-98% rotation; credential details proposed, 2026-10-08.** Configure named accounts per provider with isolated credentials. Ordinarily use one until about 98% of its relevant native quota is consumed, then rotate; make order and mark configurable. Switch earlier when the next bounded call cannot fit. Delayed readings cannot guarantee an exact stop.

Bind each attempt to immutable account/billing identity. Keep secret material outside the registry; qualify refresh and remote enrollment. Do not overwrite an active global login or fall back to pay-as-you-go credentials. Multiple credentials sharing one pool do not multiply capacity. Retain task, daily, interval and pool budgets through switching; a new account cannot enlarge an approval. Fence/reconcile the old session and preserve uncertain effects. An exhausted pool parks inference. Reserve composition follows [ADR 0017](#adr-0017), billing [ADR 0015](inference-runtime.md#adr-0015), and automatic engine failover [ADR 0022](inference-runtime.md#adr-0022). Architecture sections 8 and 14.

<a id="adr-0017"></a>
## ADR 0017: Reserve across comparable accounts

**Accepted pooled composition; qualification and taper open, 2026-10-08.** Apply the urgent reserve to a configured provider/model pool of comparable accessible accounts. This allows one account to rotate near 98% while preserving combined headroom. Include known non-build use and enforce each account's native limits plus task/daily/interval caps.

Weight by verified capacities and matching units/windows. Deduplicate credentials or models sharing one bucket. Do not average unrelated percentages or count unknown/inaccessible capacity as spare. Adding an account cannot silently expand an existing approval. Keep reservations and uncertain exposure across rotation. Exact reserve access, taper and expiry exceptions remain open. Architecture section 8.

<a id="adr-0032"></a>
## ADR 0032: Shared zero-charge inference

**Accepted profile and visible tradeoffs; capacity/quality qualification open, 2026-10-08.** Include the friend's reported four Qwen coding, two Recall fact-extraction and one BGE-M3 embedding slots as separate shared pools. Use one cross-project/test capacity ledger and protect integration-critical work. Show permitted data, work, fallback, quality and latency tradeoffs. Reported aliases and slot counts do not prove weights or sustainable concurrency; an authenticated `/v1/models` response confirmed aliases only.

Use ignored root `.env` keys `SKYBUILD_INFERENCE_BASE_URL` and `SKYBUILD_INFERENCE_SECRET_FILE`; the latter contains a path to an external token file. Keep runtime names generic and `.env.example` shareable. Start with bounded patch/structured-output requests; qualify coding and shared-load behavior before broader use. Retrieval is optional. This profile has no subscription percentage, but retains authority, resource, deadline and cutoff limits; frontier coordination/review consumes real allowance. An OpenAI-shaped protocol grants no paid API permission. The friend controls server power/models. Architecture sections 8 and 13–15.
