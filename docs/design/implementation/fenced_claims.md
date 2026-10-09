# Launch-free fenced task claims

This implements the ownership prerequisite in architecture A34 sections 5, 8 and 13. It does not implement execution admission, permits, launch journals, model/billing qualification or fleet activation. Markdown remains live task authority. Imported Markdown projects reject claim mutations, including owner/admin calls.

Source baseline: `f67c47b`. Governing file SHA-256 values are architecture `f4f2c2cbe93132a36177aa6baf6728c4a207af8f0254770c6b3e80f605533f85`, task workflow `fedc22b2fc7777879d15c3bc156166b29094ef118dd933c72a0f17292acbada6`, and MVPParallel evidence `019abd76b29a40d703c624597d3637b2c57dd9be5a7a4ec035bd6f0d3065f073`.

## Contract

Migration 007 adds a dedicated `tasks:claim` project grant, a retained ownership projection and an append-only claim journal. Claiming requires a ready task, current dependency completion and no unresolved effects. The authenticated actor becomes holder; callers cannot nominate another principal. Task revision preconditions and idempotency receipts use existing Store contracts. Ownership changes serialize under the same project graph lock as task and effect mutations.

The initial fence is 1. Each reacquisition increments it; release never deletes the record. Database triggers reject deletion, truncation, fence rollback and fence changes while ownership is held. Leases last 1–300 seconds and use the database clock. Renewal requires the current holder, live lease and exact fence; a conditional SQL update rechecks expiry. Release requires the same holder and fence, a live lease and no unresolved effects. Held claims block task edits and dependency invalidation, even after expiry, so definitions cannot change underneath ownership.

Expiry prevents renewal and effect writes but leaves ownership held. A separate owner/admin reconciliation action records an explicit reason and clears ownership only when there is no unresolved effect exposure. In this launch-free slice, the operator must establish that no process or external dispatch exists. The reason is an attestation, not machine-verifiable physical-stop evidence. Future execution requires qualified adapter/process proof before exposing this operation to automatic recovery. An expired lease never provides that proof.

Effect intent and observations retain their existing owner/admin-only restriction. If the task has a claim record, they additionally require its current holder, live lease and exact `claim_fence`. Replaying an accepted idempotency receipt returns historical data and grants no current ownership or launch authority. A historical operation-ID receipt likewise cannot recreate an effect. Unresolved intent or unknown exposure prevents claim release/reconciliation. Unknown effects remain irreversibly held under migration 006.

HTTP endpoints live beneath `/api/v1/projects/{project_id}/tasks/{task_id}/claim`: POST for acquisition, POST `/renew`, `/release`, `/reconcile`, and GET `/history`. Mutations require existing authentication, `Idempotency-Key`, and numeric `If-Match`. History is bounded and project scoped. Claims use their own journal rather than incrementing the definition revision on heartbeats.

## Evidence and limits

Disposable PostgreSQL targeted tests cover simultaneous claimers, claim versus edit, renewal versus release, expired holders, stale fences, exact replay, project/grant checks, Markdown authority, immutable journal/fence records, effect fencing, uncertain exposure and HTTP validation. Full-suite evidence is recorded in the PR after the final candidate checks.

`Claims.tla` models two workers and fences 0–3, atomic graph-locked transitions, lease expiry, delayed writes and conservative reconciliation. SANY parsed successfully. TLC with deadlock checking disabled explored 79 generated states, 46 distinct states and depth 8 without invariant violations. Parking on unresolved exposure is intentional, so the model makes no liveness claim. `Claims-broken.cfg` deliberately clears ownership/exposure at expiry; TLC reports `ExposureRetained` violated at depth 4 (11 generated states, 8 distinct states). This confirms the exposure invariant is exercised.

Reproduce from `docs/design/models` with `rtk proxy sany Claims.tla`, then `rtk proxy tlc -deadlock -workers 1 -metadir /tmp/skybuild-claims-check -config Claims.cfg Claims.tla`. Substitute `Claims-broken.cfg` and a different scratch directory for the expected-failure check. These bounded results are design evidence, not a proof of PostgreSQL, physical process stopping or clock behavior. No runtime launch or live cutover is authorized by these APIs.
