# Effect reconciliation before structural replacement

This bounded design model implements no runtime behavior. It refines the existing A33 requirements in architecture sections 8 and 13, ADR 0030, and the task workflow's structural-change and immutable-history contracts. Markdown remains the live task authority. No worker, service, migration, authority switch or external operation is started by the model.

## Source boundary

The inspected base is `7a64ffb23ede6efe0e7b8f300fd58bdbf47a5c24` on `origin/dev-001`. SHA-256 source digests are:

- `docs/design/architecture.md`: `dccd30d1c1bc0d6c0b951c6ede9f1aa06795f1e1db5565e4dd7aac40e3082339`.
- `docs/design/task_workflow.md`: `fedc22b2fc7777879d15c3bc156166b29094ef118dd933c72a0f17292acbada6`.
- `src/skybuild/store.py`: `83a53ed73fc5cff6d5f4a349f595a81d3e3258ba2ac247a9dd7b6fc3e899e086`.

`Store.split_task` and `Store.merge_tasks` currently require proposed sources and affected dependents with no started history. `_has_started_history` conservatively checks the journal for working/integrating phases and in-progress/done status. That restriction remains in place. A model does not qualify an effect adapter or justify removing the restriction. No architecture or implementation-plan sequencing is changed.

## Safety statements

1. A dispatched operation that can still start or remains running retains its admission exposure.
2. Structural replacement cannot commit while an original operation can still start or remains running.
3. Every created operation retains its stable record after cancellation, terminal reconciliation or replacement.
4. Once change reconciliation starts, no result advances acceptance for the original definition. Historical acceptance remains historical evidence.

The model distinguishes controller observation (`intent`, `unknown`, `active`, `terminal`, `cancelled`) from external reality (`none`, `flight`, `running`, `ended`). `flight` is an issued request that can still start. It is necessary to represent the race where a scope edit fences new dispatch while an earlier request starts afterward. A lost response, lease expiry, restart or stop request cannot establish `ended`.

`RequestChange` closes dispatch and intent creation for the original definition. It does not stop an already issued request. Never-dispatched intents can then be cancelled under the same serialized boundary. Unknown and active operations become terminal only when the adapter proves completion or proves an in-flight request cannot start. Only after all exposure is resolved can `Replace` commit. External starts and finishes remain possible during reconciliation.

`accepted` is a small abstraction for acceptance advancing the original definition; it is not the completion API or a claim of accepted publication. Stable operation records are retained in `recorded`. Result payloads, immutable event contents, numeric usage, budgets, task graph coverage and replacement dispatch are not modeled. `held` represents unresolved admission exposure, not refundable spend: real accounting must retain consumed usage across lineage even after terminal reconciliation.

## Reproduction and results

Run from the repository root, with installed TLA+ wrappers:

```sh
sany docs/design/models/EffectReconciliation.tla
tlc -workers 1 -metadir /tmp/skybuild-effect-safe-states docs/design/models/EffectReconciliation.tla
tlc -workers 1 -metadir /tmp/skybuild-effect-broken-states -config docs/design/models/EffectReconciliationBroken.cfg docs/design/models/EffectReconciliation.tla
```

SANY completed semantic processing without errors. TLC 2.19 exhaustively checked two fixed operation identities: **514 generated states, 197 distinct states, search depth 13**, with no error. The safe configuration checks `TypeOK`, `UncertainHeld`, `ReplacementSafe`, `RecordsRetained` and temporal property `NoLateAcceptance`. Deadlock checking is disabled because terminal and safely parked states are intentional. No fairness assumption or state constraint excludes uncertainty.

The deliberately broken configuration allows an unknown observation to release exposure without authoritative external evidence. It checks `ReplacementSafe` directly, omitting `UncertainHeld` so TLC continues past the earlier exposure failure to exhibit the structural hazard. TLC exits 12 with **80 generated states, 54 distinct states, depth 6** and this counterexample:

1. Initial state has no operations.
2. Commit `op1` intent and hold its exposure.
3. Dispatch `op1`; observation is unknown and external request is in flight.
4. Request structural change, closing new dispatch.
5. Incorrectly mark unknown `op1` terminal and release its exposure.
6. Replace the task while `op1` can still start.

An earlier check including `UncertainHeld` rejected the same mutation immediately at step 5 (four states before the change request was included). The committed broken configuration intentionally demonstrates replacement failure rather than stopping at that earlier diagnostic.

This is exhaustive checking only for the stated finite abstraction. It is not an implementation proof, adapter qualification, liveness guarantee, cancellation guarantee or exactly-once external-effect claim.

## Required implementation mapping and remaining choices

- Every effect must commit a stable operation ID, task/attempt, authority epoch/generation, input/policy digest, allocation references and dispatch intent before external I/O. Effects outside that registry invalidate the model's completeness assumption.
- Intent creation, dispatch authorization and structural-change fencing must serialize against the same affected task/definition boundary. No database transaction spans network I/O. For an already authorized local launch or remote request, durable uncertainty must precede I/O; the race between authorization and send remains held, never silently classified as an undispatched intent.
- `ConfirmNotStarted` requires adapter-specific authoritative evidence that no delayed launch or request can start. A current lookup reporting no process is insufficient if a queued request can still arrive. `ExternalFinish` similarly requires terminal evidence for the stable external identity. A lease identifies the reconciler and does not prove either condition.
- Terminal/cancelled observation and exposure resolution must commit atomically with append-only journal evidence. Duplicated reports resolve the existing operation, not another operation. This model permits at most one intent per stable ID and does not exercise arbitrary delivery payload conflicts.
- The structural transaction must recheck the exact definition revision, full effect set and current fence before changing lineage/dependencies. Do this for every source and rewired dependent, not only the task whose page initiated the change. Preserve all consumed and unresolved allocation history; split/merge cannot reset usage.
- Late results attach to the original operation and definition. They cannot satisfy replacement evidence. The model gates acceptance after the change request; durable storage still needs exact source/evidence generations and stale-result events.
- Next implementation choices remain adapter proof formats, effect persistence schema, reconciliation claim/fence rules, pending-change request payloads and cancellation behavior, invalidation integration, and the first qualified CPU adapter. Existing proposed-only structural guards remain until those choices and disposable PostgreSQL concurrency tests receive independent review.

Required implementation tests include a change racing dispatch; delayed start after change pending; lost start/merge acknowledgement; timeout and lease expiry retaining exposure; terminal evidence racing structural commit; duplicate/stale reconciliation; source and dependent effects during split/merge; and uncertain usage preserved across replacement. Readiness and completion evidence remain separate gates.
