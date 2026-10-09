# Read-only frozen ledger audit

`python -m skybuild ledger-audit` compares the reviewed frozen importer projection with the current three Markdown task ledgers. It reads Git blobs at the contract's exact commit into a temporary directory, then invokes the existing importer validation and projection. The baseline must still match the contract's source hashes, manifest hash, explicit dependency mapping and record bounds. Current files are parsed separately. No database configuration or database connection is used.

The report identifies added/removed IDs, normalized title/status/priority/acceptance/architecture changes, source moves, original status text and raw-section hashes. Source hashes also expose changes outside task sections. It retains the frozen import-plan digest for review. Phase, next action, responsible person and other conservative importer defaults still need independent field reconciliation.

`dependency_reconciliation` has one row per current task. It preserves every Dependencies bullet line exactly as text (without its line ending), lists distinct literal `SKYBUILD-*` mentions that resolve to current task IDs, and flags unknown IDs, self references, repeated mentions and remaining prose. Empty `dependency_lines` means no Dependencies line was found. An unchanged ID also gets its frozen explicit dependency array and frozen line evidence, a line-change flag, and `literal_only`/`frozen_only` ID differences. Added IDs have `frozen_comparison = null`. These are lexical observations, not proposed edges: even a literal mention can be conditional, and prose needs human interpretation. The validated frozen mapping remains the import contract; this report neither revises it nor generates a replacement.

Exit 0 means current bytes match the frozen source, not cutover acceptance. Exit 2 emits a report with `stale_freeze = true` and an explicit `STALE FREEZE` warning. Exit 1 means validation failed. Every successful report retains `authority = markdown` and `cutover_ready = false`. A stale freeze needs a new identified source freeze, independently reviewed mapping and digest before a future import. This tool does not generate or accept that freeze.

This audit is only preparation for architecture sections 4 and 7. Database rows, histories, REST reads after restart, source-writer fencing, running-effect reconciliation and explicit authority/recovery acceptance remain separate work. Matching source bytes alone never authorize API writes. No authority manifest, ledger, live database, worker or service changes occur.

## Bounded source evidence and validation

The dependency reconciliation slice uses architecture A35, sections 4 and 7, the frozen import contract at `6d96075f88493d0b54577a2a8c9526f19a78a5ed`, and task-branch base `54466fe54bf4132ca7dbbe2fcbe12c697d539676`. Fifteen targeted audit tests pass. The report remains advisory and cannot accept cutover.

The assignment reads architecture revision A34, working contract and sections 4/7. Architecture SHA-256: `f4f2c2cbe93132a36177aa6baf6728c4a207af8f0254770c6b3e80f605533f85`. Frozen contract SHA-256: `53054f65ce8a273eb86f59acc6fcb1897be77d05b09db580eb2ec48265c9fc8f`. Frozen source commit: `6d96075f88493d0b54577a2a8c9526f19a78a5ed`.

At this branch's baseline, current Markdown has 30 tasks versus 28 frozen tasks: two quality tasks added, four existing sections changed, and no removed IDs. The audit correctly reports a stale freeze; the old importer remains strict and refuses current ledger bytes.

Fourteen focused ledger/audit tests pass, covering unchanged validation, normalized/raw/header drift, tampered contract refusal, added/removed/duplicate IDs, source immutability and stale CLI exit without a database environment. Independent review is recorded in the pull request before integration.

The existing importer test fixture now materializes the pinned Git blobs in a temporary checkout instead of reading mutable live ledgers. This restores rehearsal tests after legitimate source drift without relaxing importer validation. The full local suite passes 94 tests with 102 PostgreSQL-dependent tests skipped and one existing Starlette warning. No PostgreSQL service was started for this slice.
