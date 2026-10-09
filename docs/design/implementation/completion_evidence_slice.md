# Manual code-task completion evidence

This launch-free slice implements an owner/admin attestation boundary for code tasks. It does not implement a publisher, external evidence collector, reviewer qualification service, worker, or authority cutover. The live Markdown task ledgers remain authoritative. Imported projects whose receipt says `authority = markdown` reject this operation through the existing Store write guard.

## Governing sources

Architecture A33 sections 2, 4, 5 and 13 govern publication, task authority, append-only workflow and independent review. Source SHA-256 values for this brief are:

- `architecture.md`: `dccd30d1c1bc0d6c0b951c6ede9f1aa06795f1e1db5565e4dd7aac40e3082339`.
- `task_workflow.md`: `fedc22b2fc7777879d15c3bc156166b29094ef118dd933c72a0f17292acbada6`.
- `review_policy.md`: `2a3e453bf6197babbd5f5490987f87ad8886a5efcda492f438b940f4c9dfb0cc`.

The current handoff identifies acceptance/review/publication evidence before completion as the next implementation boundary. This slice preserves the existing model: ordinary task edits cannot write `status`, computed workflow state, or reserved completion evidence.

## Contract

`POST /api/v1/projects/{project_id}/tasks/{task_id}/complete` requires bearer authentication, `If-Match` and `Idempotency-Key`. Only a current owner/admin can attest completion. The payload contains:

- A reason, current workflow generation, exact source head, author identity and accepted policy reference.
- One evidence reference for each exact, ordered acceptance criterion.
- One or more named passing checks against the exact source head.
- A passing independent review against the same head, reviewer identity distinct from the author, review session/report references, and zero unresolved blocking findings.
- Confirmed publication of that task head, tested candidate/base and target commits, target branch and a reference confirming task inclusion. Candidate and target hashes may differ under squash/rebase; matching hashes alone do not establish publication.

All text and lists are bounded. Partial, unknown, failed and mismatched evidence is rejected. Deferred or superseded tasks cannot complete; completed tasks must be reopened first. Dependencies require current accepted completion, not a historical status label. State, evidence snapshot and journal commit atomically under existing graph and row locks. Retries return the original accepted response; a different payload with the same key conflicts.

The completion snapshot records schema version, `kind = owner_attestation`, authenticated actor, original input revision, definition and supplied evidence. Task history retains the original snapshot after reopening or definition changes. `current_completion()` checks status, definition and workflow generation; imported historical completion alone is not a fresh attestation. Reserved `_skybuild_completion` metadata cannot be supplied through create/update.

## Trust and remaining work

This endpoint stores an explicit trusted owner statement. It does not independently fetch logs, verify reviewer model qualification/session isolation, compare Git trees, authenticate third-party evidence, or confirm GitHub publication. A worker with ordinary `tasks:write` cannot submit accepted attestations. The owner remains responsible for checking the evidence references and accepted policy until trusted collectors are implemented. Do not advertise this as automated completion verification.

Automatic task result acceptance, policy-specific required-check selection, bundle gates, publication intent/reconciliation, effect fencing, stale external results, and non-code acceptance without integration remain later slices. Completion does not grant execution, admission, model spend, deployment, cleanup or publication authority.

## Validation

Targeted tests cover stale generation and acceptance, missing checks, changed source heads, failed checks, self-review, unresolved findings, pending publication, invalid object IDs, owner-only HTTP access, atomic failed requests, revision conflict, idempotent replay, metadata injection, dependency acceptance and retained evidence after rework. Tests run only against a task-owned disposable PostgreSQL database.
