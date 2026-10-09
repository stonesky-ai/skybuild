# Launch-free effect intent persistence

This bounded slice derives from architecture sections 5, 8 and 13 and the EffectReconciliation model. It implements storage and serialization only. Markdown remains live task authority. Do not use this scaffold to authorize or dispatch live work.

## Source boundary

Implementation began from `origin/dev-001` at the worktree's branch point. The architecture source SHA-256 is `f4f2c2cbe93132a36177aa6baf6728c4a207af8f0254770c6b3e80f605533f85`; the model explanation SHA-256 is `561076b35a2987052503c8e465a8bab9ca4d33b65251b46db548ee917163c40f`.

## Implemented boundary

Migration 006 adds stable globally unique operation identities, task/attempt association, exact task revision, authority epoch/generation, input/policy digests, allocation references, intent fingerprint and held exposure. These supplied references are recorded claims, not verified admission, allocated resources, reservations or launch authority. The existing owner/admin authorization and Markdown import write guard apply to all writes. No new API route, CLI operation, adapter or worker exists.

`Store.create_effect_intent` commits intent and an append-only effect event together. A duplicate operation ID with identical intent returns the existing operation, even after cancellation. Conflicting reuse fails. Ordinary idempotency receipts remain historical outcomes; replaying an old intent receipt cannot restore the operation's current state.

`Store.observe_effect` can mark intent unknown or cancel a never-exposed intent. This codebase has no dispatch path; any future dispatch must durably transition intent to unknown under the same project graph lock before external I/O. Cancellation and unknown observation serialize. Cancelled operation identities cannot be reactivated. Unknown operations retain exposure indefinitely: elapsed time, lease expiry, task editing and ordinary admin actions cannot clear it. Qualified adapter proofs and terminal reconciliation are deliberately absent.

All task replacements and dependency invalidation reject held exposure under the existing project graph lock. A source edit that reaches an exposed dependent rolls back entirely, including prior writes and journal events. Split/merge keep existing proposed-only and no-started-history restrictions; unresolved source or dependent effects also prevent the transaction from committing. This conservative scaffold blocks edits instead of storing pending changes. It does not implement the model's pending-change lifecycle.

Operation records cannot be deleted/truncated or have immutable identity fields changed through ordinary SQL. Effect history rejects update/delete/truncate, following the existing journal protection. Database owners remain privileged and can change schema; runtime least-privilege provisioning remains a separate boundary.

## Validation and limits

The original bounded model was rechecked with SANY and TLC: 514 generated states, 197 distinct states, depth 13, no error. Its deliberately broken reconciliation configuration fails `ReplacementSafe` with the six-state trace where an in-flight operation loses exposure and replacement commits. These model results check the existing finite design abstraction, not this implementation or adapter behavior.

Disposable PostgreSQL tests cover stable duplicate identity, conflicting reuse, uncertain exposure, cancellation and historical retry, task edits, dependency rollback, intent/edit and cancellation/unknown races, split/merge source and dependent exposure, write authority, and append-only records. Full importer fixture checks require the separate pinned-source fixture repair in PR-010; no frozen ledger snapshots were changed here.

Remaining work includes verified authority/attempt/allocation records, dispatch admission, pending changes and visible blockers, qualified reconciliation/terminal proof formats, numeric accounting, external identity and results, source generation binding and physical stop/launch qualification. Unknown exposure cannot be resolved through this scaffold. There is no exactly-once external-effect claim.
