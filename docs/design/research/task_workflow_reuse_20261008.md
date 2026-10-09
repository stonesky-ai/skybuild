# Task workflow and merge preparation reuse

Read-only inspection on 2026-10-08 for architecture A30. CodeGraph status confirmed `/home/kevin/my_code/skykeep`; the index reported pending additions, so indexed absence is not evidence of missing code. Exact-file exploration returned current on-disk source. No mergeprep action, Git fetch, lint, queue update or service was run.

Checkout HEAD: `383d3d375979c66b39df0f61c19a165957fecdcf`. Inspected `scripts/mergeprep.py` SHA-256: `4bfedd50719166bcc09e075bd907139f33d752732ed7f7fd6ef1ffeb9df6cdfa`. Source checkout/hash is evidence, not an installed/deployed version claim.

Useful existing behavior:

- `lane_fact` (around line 525) turns lane-evidence problems into explicit facts rather than a generic refusal.
- `seam_facts` and `decide` (around lines 610–689) separate collected facts from readiness reasons: brief/revision, source relationship, conflicts, scope, lints and evidence coverage.
- `check_seam` (line 691) emits exact tip/trunk, status, reasons and related work identifiers.
- `stale_record` (line 705) records why reassessment has not happened and the previous status/base.

Adapt those fact/verdict/staleness concepts into the shared task readiness predicate and structured next actions. Retain the failure categories in migration/acceptance fixtures so the mergeprepper's known diagnoses are not rediscovered by models. Do not copy old Git-ref/JSON publication as another writable task authority; new task history and readiness belong to the accepted PostgreSQL domain. The inspected code's global usage guard also needs effect-specific review so CPU reassessment does not depend on model availability.

The excerpt establishes useful reuse candidates, not exhaustive mergeprepper behavior or end-to-end workflow coverage. Inventory the role skill, callers, full reason taxonomy, tests and deployed version when this area is selected. The new owner requirement adds continuous task history, structural task edits, durable invalidation, next-action ownership and explicit bundle progression; it is not already satisfied merely by a readiness scan.
