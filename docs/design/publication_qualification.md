# Publication qualification and launch-free acceptance contract

Status: unqualified for live automatic publication. PR-014 records a narrow contract and future acceptance evidence, not a publisher implementation or permission to change GitHub authority.

## Governing inputs and observed repository state

This note derives from [architecture A34, section 2](architecture.md#automatic-bundled-integration), [implementation plan areas 5 and 5a](implementation_plan.md), [ADR 0030](../adr/0030-effect-boundaries-and-accounting.md) and the [publication model](models/Publication.md), at source revision `a454f09bcd5a634445790ad0df7ab8ddebf58ccf`. The model assumes atomic remote base enforcement; its sampled traces do not qualify GitHub or establish liveness.

Read-only REST inspection on 2026-10-08 observed `stonesky-ai/skybuild`: `GET /repos/stonesky-ai/skybuild/branches/main` returned `protected: false`; `GET /repos/stonesky-ai/skybuild/rulesets` returned an empty array; repository permissions for the current credential were `push: true`, `admin: false`, `maintain: false` (also `pull: true`, `triage: true`). These observations are time-bound, not authority guarantees. They do not establish protection for `dev-001`, applicable organization policy, bypass behavior, or plan support. Qualification must inspect the actual configured target again. No rules, credentials, branches or administrative permissions were changed by that inspection. No administrator request is part of this contract.

## What GitHub documentation establishes

- The [merge REST endpoint](https://docs.github.com/en/rest/pulls/pulls#merge-a-pull-request) accepts `sha` as an expected PR head. Its documented request has no expected-base compare-and-swap parameter. A preflight target read cannot close the race after that read.
- [Asynchronous merge](https://docs.github.com/en/rest/pulls/pulls#merge-a-pull-request-asynchronously) accepts work with HTTP 202 and an operation UUID. The [result endpoint](https://docs.github.com/en/rest/pulls/pulls#get-the-result-of-an-asynchronous-merge) retains results for 24 hours after their latest update. `enqueued` is a final queue-enrollment result, not a completed merge; an expired UUID returning 404 is not proof that publication failed.
- [Protected branch documentation](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches) distinguishes strict up-to-date required checks from loose checks and supports an expected check source. The [branch protection API](https://docs.github.com/en/rest/branches/branch-protection#update-branch-protection) exposes `strict` and required-check `app_id`. A check name alone is insufficient trusted provenance. Actual configuration, actor permissions and bypass paths still need evidence.
- A [merge queue](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue) tests a temporary candidate containing the current base and preceding queued changes. Required CI must handle `merge_group` or the corresponding queue branch. PR-head checks alone do not attest that queue candidate.

Strict protected PR merging and merge queues are candidate mechanisms, not selected or qualified flows. Strict mode alone does not prove that the accepted check attests the exact frozen bundle/base/policy. Queue recomposition requires new candidate evidence and membership reconciliation.

## Narrow fake-adapter contract

Develop only deterministic local contract tests and fake adapters. Do not contact GitHub for writes, start services, launch workers, change repository authority or grant credentials to candidates.

The frozen candidate record identifies repository/target, base commit, member task IDs and exact heads, combined tree/commit, dependency order, accepted policy revision, required check union, trusted result source, review references and environment provenance. Changed inputs invalidate the record. New tasks wait for the next bundle.

Before external I/O, atomically persist one effect intent and exclusive unresolved slot per repository/target. Intent carries stable effect/idempotency identity, owner generation, exact candidate/policy references, dispatch state and later remote operation references. Dispatch validates current generation, authority, evidence and slot ownership. Commit intent before send; a crash around send remains uncertain until reconciled. The trusted adapter must provide an explicitly qualified remote guard for the tested head/base relationship. A fake guard must reject a changed base atomically with apply; it must not hide a local read followed by an unconditional write.

Use explicit states: prepared, dispatched-unresolved, confirmed-applied, confirmed-not-applied, and conflict-needs-reconciliation. Accepted, queued, timed-out, lost-response, stopped, replaced-owner and expired-lookup observations cannot establish confirmed-not-applied. They retain the slot. A new owner may reconcile the existing effect but cannot dispatch stale ownership or create a competing publication. An already-dispatched operation may complete after stop.

Reconciliation establishes actual remote merge/target ancestry or content relationship, expected base/candidate relationship and each member task inclusion. Store remote operation/result references and observation timestamps. Release the slot only with durable conclusive outcome evidence. Confirmed rejection permits a newly prepared candidate only under current authority; confirmed application permits acceptance and cleanup only for verified members. Inconclusive or contradictory observations retain pending state, named blocker and responsible integrator. Do not retry an uncertain mutation merely because lookup expired.

## Launch-free acceptance checklist

All items below require retained test names, exact source revision, commands, actual results and relevant trace/assertion artifacts. Unticked items are requirements, not completed validation.

- [ ] Freeze exact member heads/base/policy and union of required gates; reject changed members, candidate edits and stale review/check evidence.
- [ ] Persist intent before send and serialize two competing integrators on one target; reject stale generation immediately before dispatch.
- [ ] Inject a crash before send, after possible send and after apply before acknowledgment; restart reconciles the same effect without unsafe duplicate publication.
- [ ] Inject target advancement between local validation and fake remote apply; atomic remote guard rejects the stale base without changing target.
- [ ] Exercise accepted 202, duplicate/pending operation reference, queue enrollment, delayed completion, timeout, expired-result 404 and contradictory reads; all uncertain cases retain the same slot.
- [ ] Replace owner and issue stop while an effect is unresolved; no second publication becomes eligible, while later confirmed application remains reconcilable.
- [ ] Verify trusted check origin, candidate identity and policy binding; forged same-name results, wrong app identity and candidate-controlled policy fail.
- [ ] Recompose a queue candidate or split a failed bundle; require new evidence and verify inclusion/exclusion for every task.
- [ ] Release slot and permit task acceptance/cleanup only after durable conclusive outcome and exact per-task inclusion evidence; repeat reconciliation idempotently.

## Evidence required before any live adoption

Retain a dated qualification packet for the exact repository, target, actor and selected flow. Include plan/feature support; complete applicable branch/ruleset configuration and bypass actors; actor credential scopes without secrets; required check names and app identities; accepted policy provenance; isolated candidate credential boundaries; merge method and exact candidate/base mapping; remote operation/result samples; and conclusive target/member reconciliation evidence. A missing field remains a named blocker. Never infer capability from push permission or a successful manual merge.

Run a separately authorized changed-base experiment in an isolated qualification repository or target: gate candidate C against base B0; pause after local validation; advance target to B1 through a competing writer; submit the exact frozen request. Record B0/B1, head/tree hashes, request parameters, check source identities, applicable rules/bypasses, operation IDs and final target/member observations. Passing means stale B0 evidence cannot publish onto B1. Rejection is acceptable; rebuild/recheck must produce new evidence for B1. Repeat at the actual remote boundary and queue recomposition path, including bypass-capable actors and same-name wrong-source check attempts. A local fake success is insufficient live evidence. No such experiment was run for this note.

Unresolved choices: concrete remote mechanism; strict-check candidate attestation; queue candidate/membership mapping; qualified result retention and reconciliation sources; safe terminal-negative evidence; bounds for operator escalation without slot release; administrative/plan feasibility; and current target bypass policy. These block live automatic publication, not launch-free fake-adapter development. Slot retention is a safety rule, not an eventual-progress guarantee.
