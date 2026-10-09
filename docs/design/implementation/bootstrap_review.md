# Bootstrap independent review

Date: 2026-10-08. Reviewer: the separate `bootstrap_review` model session. Final result: **pass for the corrected local bootstrap and thin workbench candidate identified by the final combined manifest below**. R1, R2 and R3 are resolved. The reviewer authored none of the reviewed production code, migrations, tests or packaging; the reviewer owns only this report.

The review first accepted a 104-test bootstrap-only snapshot. The API subsequently gained workbench wiring while this report was being written, requiring the additional review and correction recorded below. That earlier acceptance is retained as history; its API hash does not cover the wiring, and R3 subsequently superseded its Store acceptance. Final combined acceptance follows the correction records and uses the final manifest, not the earlier manifest.

The governing inputs are [architecture A33](../architecture.md), sections 3–7 and 13, the [bootstrap brief](bootstrap.md), and the [review policy](../review_policy.md). The repository HEAD during review was `2da489c992f55e3484e475bdf3a175a97e1d90c7`; the new implementation was uncommitted. HEAD alone therefore does not identify the accepted candidate. The SHA-256 manifest below does. Changes to those sources or governing contracts invalidate affected acceptance.

## Scope and evidence

Review covered the Store and migrations, authentication and project scopes, task revisions/dependencies/history, Cord transitions and reply authority, shared identifiers, HTTP validation and error handling, client retries, explicit CLI administration, packaging, the read-only ledger manifest helper, and all five bootstrap test files. The review challenged transaction failures, concurrent cycle formation, duplicate mutations, stale revisions, recipient isolation, credential replacement, secret exposure, input bounds and unnecessary behavior.

The lead supplied successful applicable checks before initial review: API/client 40 tests, Store 35 tests, ledger 7 tests, and HTTP/PostgreSQL 3 tests. After correction, the lead reported 104 passing combined tests. The reviewer independently ran the corrected, explicitly bounded suite:

```bash
SKYBUILD_TEST_DSN=<owned-disposable-store-dsn> \
SKYBUILD_HTTP_TEST_DSN=<owned-disposable-http-dsn> \
rtk proxy .venv/bin/python -m pytest \
  tests/test_store.py tests/test_api.py tests/test_client.py \
  tests/test_ledger.py tests/test_bootstrap_integration.py -q
```

Reviewer result: **104 passed, 1 warning in 16.92 seconds**. The warning is the upstream Starlette/FastAPI TestClient deprecation of its httpx integration. Validation used only the task-owned PostgreSQL container: `skybuild_test`, `skybuild_http_test`, and the automatically removed `skybuild_test_upgrade_*` database created by the migration-upgrade test. No production database or inference endpoint was contacted.

Relevant checks exercise wrong-database refusal, persistent task state, authenticated project and operation boundaries, credential replacement, conditional writes, actor/project/operation idempotency, concurrent dependency-cycle attempts, database journal immutability, reply authority through both entry points, transactional reply/handling, and injected Cord event-write failures. The process test starts the explicit CLI on loopback against the disposable database, terminates it, starts a fresh process, and verifies the same task. This is stronger than app recreation alone, but does not establish PostgreSQL crash recovery or deployment availability.

## Finding and disposition history

### R1 — Required, P2: Cord mutations lacked immutable actor history

Initial affected revision: `src/skybuild/store.py` SHA-256 `5fbdb3b9e102eae4b66615196cb518e20daa6b2a4e0468ab2d8607227ac21f70`, especially `_insert_message` and `message_action`, original lines 381–385 and 410–423. Migration 001, original lines 74–95, had only the mutable message projection.

Demonstrated contract gap: an administrator could handle a message addressed to a worker, changing `handled_at`, without an immutable history event identifying the administrator or the prior/new state. The actor existed only in the mutable idempotency result table. This failed the brief's server-derived actor journal requirement and architecture section 5's transactional mutation-history requirement.

Required correction: append Cord actor/action/state events within the same transaction as message changes. Retain original/reply identity and separate reply from optional handling. Protect event UPDATE, DELETE and TRUNCATE in PostgreSQL. Same-key replay must add no events; failed event insertion must roll back message state, replies and idempotency results.

Author disposition: migration [002_cord_journal.sql](../../../src/skybuild/migrations/002_cord_journal.sql) adds the append-only Cord journal. Store `_cord_journal`, `_message_transition`, `_insert_message` and `message_action` record authenticated actors, prior/new projections and reply linkage. Combined reply/handle operations append separate transitions. Migration 001 remains unchanged, and upgrading does not fabricate historical actors for old messages.

Independent disposition: **resolved**. The reviewer inspected the corrected transaction paths and migration, then reran the passing suite. `test_cord_journal_records_actors_and_distinct_reply_handling` verifies the administrator actor, reply linkage, state distinctions and replay event count. Database mutation tests reject UPDATE/DELETE/TRUNCATE. Parameterized failure injection covers send, receipt, handle, reply and combined reply/handle, including rollback of state and idempotency. The 001-to-002 upgrade test preserves existing records and permits repeat migration.

### O1 — Optional: required-text validation differed between HTTP and Store

Initial Store paths accepted empty descriptions and message bodies while HTTP rejected empty strings. The authors chose nonempty required text and aligned Store behavior. Focused Store tests cover both empty and whitespace-only values. The HTTP-to-Store path also preserves Store validation. No required finding remains.

### O2 — Optional: excessive JSON nesting returned unavailable

The reviewer reproduced an authenticated task request with 1,000 nested metadata arrays returning sanitized HTTP 503 before correction. No mutation or secret exposure was demonstrated.

Author disposition: the HTTP body boundary now rejects nesting beyond 64 containers before JSON decoding, while ignoring quoted/escaped UTF-8 text. Focused tests retain sanitized 503 for unrelated Store failures rather than misclassifying those as request validation. Independent reproduction of the original nested request now returns the standard HTTP 422 validation envelope. No required finding remains.

## Bootstrap-only acceptance boundaries

This pass accepts only the reviewed local bootstrap code and isolated validation. It does not declare the whole bootstrap, API authority cutover or self-building MVP complete. Tailscale/browser reachability, production deployment credentials and database role separation remain unqualified. The runtime role must not have migration-administrator privileges or authority over SkyKeep databases. No deployment, live migration, worker/fleet start, model call, billing change or Git publication is accepted by this report.

The ledger helper and `ledger-manifest` CLI preserve raw task sections and source hashes. They do not resolve prose dependencies, import tasks, freeze live authority or perform cutover. The Markdown ledgers remain authoritative. Offset pagination is an ordered bounded view, not a snapshot or commit-order feed; inbox consumers must repeatedly scan pending messages under the selected bootstrap contract.

The later web workbench (`web.py`, static assets and `test_web.py`) was being developed in separate files and was not wired into the reviewed API. It is excluded from this acceptance and requires its own checks and independent review. The reviewer found no additional required correctness, authorization, transaction, idempotency or concurrency defect in the identified bootstrap snapshot. This is bounded review evidence, not a guarantee against all defects.

## Accepted bootstrap-only SHA-256 manifest

```text
6dc62b0e7d135b949d66c97b99c12155a4ffa60d822b994b9a5d109b1ebe9af1  src/skybuild/__init__.py
3c9a4462d25472e126bb54587ff4f9ff43cc3e03d2cbe2ece059406aee90b401  src/skybuild/__main__.py
7ba6ba5e712dc2eb87209fdf58fca3e68dc9fbd42d3f896135d6f7266c48fd11  src/skybuild/api.py
0b376ab630a71c7f930e2b67b0fdcadc99a69d4f2bfb07ce3bf68a4ad4c8fd0e  src/skybuild/client.py
8124412e09d5a403e9fce82212b2a9bb2e87ac6a6b21a76f977011f18adf93ae  src/skybuild/contracts.py
bb076ffc4da1a30fd608424e6507e3d3eebbfc7168abcfd597cda6c23657cc52  src/skybuild/ledger.py
2eb96152129cef7ed7268429fcc8dffa6e1456d0205275a1374c59b2584da3fb  src/skybuild/store.py
75b653aba28c7279e4b4b08ca186ddd193c1af086d5e320c6aa09258ca42d9ae  src/skybuild/migrations/001_bootstrap.sql
1b4040897e99a1102f841848000a30f9cf95b7f37a4de0d4a859569be8fd9906  src/skybuild/migrations/002_cord_journal.sql
6e2082bf77c67568aeb6489c3526456379b0ed4faf19a732cd823447d6e3b584  tests/test_api.py
7f3cb98305e7f3778f53665701ffc071bf2d8b924663a3350a984616e3e39fae  tests/test_store.py
b3776ccc2cf9155d970e3f54f67e4de4c19778e3f00ddcae4a303e6e595e0b86  tests/test_client.py
548ab65741dbd5f49c3faf4181f5656bc87978dc31b9f0ee3679b23f87e398a4  tests/test_ledger.py
b0149aa84a6c369868698b4e777e11363a6da01e613dec7801a5657d4f194d66  tests/test_bootstrap_integration.py
f2384aa9239dbb698ccca4028321f038d719878f8177e87ff070738d95f9360b  pyproject.toml
6c5bda99bbd7ba7f0a5d501d7385e583573d8b68b73943c58ea0093524765e0d  uv.lock
dccd30d1c1bc0d6c0b951c6ede9f1aa06795f1e1db5565e4dd7aac40e3082339  docs/design/architecture.md
2a3e453bf6197babbd5f5490987f87ad8886a5efcda492f438b940f4c9dfb0cc  docs/design/review_policy.md
e6aab21c300cfc2c7fe4437932bcbad4f3d55d9a865465a37e69eaf7a0b6941a  docs/design/implementation/bootstrap.md
```

## Additional workbench review

The lead extended review scope to the API's workbench import/installation, `web.py`, its three static assets and `test_web.py`. This is a new review of the combined candidate, not retroactive coverage by the bootstrap-only acceptance. The reviewer has not edited those sources.

### R2 — Required, P2: saving a task changes opaque dependency IDs

Affected initial workbench revision: `src/skybuild/static/workbench.js` SHA-256 `126f116d826f9433ec7a5f19d33d388741e6cf23e37b378242bca13b709c2f7d`, edit submission at line 164.

The dependency input applies `.trim()` to every line before PATCH. The shared identifier contract deliberately preserves leading and trailing spaces. With distinct tasks named ` dep ` and `dep`, opening a task depending on ` dep ` and changing only its title silently replaces its dependency with `dep`. If `dep` does not exist, the otherwise valid edit fails instead. This is a demonstrated source-level data-integrity defect, not a cosmetic whitespace preference.

Required correction: preserve each nonblank dependency line exactly. Use trimming only to detect blank lines, or use structured identifiers. Add a browser regression with both distinct task IDs, save an unrelated field, and verify the dependency and journal projection retain ` dep ` exactly.

Disposition: correction and independent re-review pending. The combined workbench candidate is not accepted while R2 remains unresolved.

### R3 — Required, P2: literal Unicode escape text is rejected as NUL

Affected Store revision: SHA-256 `2eb96152129cef7ed7268429fcc8dffa6e1456d0205275a1374c59b2584da3fb`, `_json`, line 51. This predicate predates the workbench delta; the reviewer demonstrated the defect during the extended review.

The serialized JSON substring check for `\u0000` also matches harmless input containing the literal six-character backslash-u0000 sequence. For example, a description explaining that escape raises `validation: JSON must not contain null characters`, although the description contains no NUL. The same false rejection affects metadata values and keys. Code/documentation task text is legitimate input under the selected text contract.

Required correction: detect actual NUL characters in original string values and keys, or distinguish escaped backslashes correctly. Preserve literal escape text through create, update, metadata and HTTP, while retaining actual-NUL rejection. This is an input-preservation defect; no data corruption or secret exposure was demonstrated.

Disposition: correction and independent re-review pending. R3 also blocks acceptance of the combined candidate.

### Corrected R2 disposition

The author changed dependency parsing to preserve each nonblank line exactly; trimming is used only as the blank-line predicate. `test_edit_handler_preserves_dependency_and_project_identifier_whitespace` executes the actual JavaScript connection, selection and edit handlers using Node with a bounded DOM/transport fixture. It verifies exact dependency values, encoded project/task whitespace and the original revision header.

Independent disposition: **resolved**. The reviewer inspected the corrected handler and regression, and the test passed in the combined reviewer run without being skipped. The lead also reported a real Chromium/PostgreSQL browser scenario with distinct ` dep ` and `dep` tasks: an unrelated edit preserved the original dependency and journal state exactly.

### Corrected R3 disposition

The author replaced the serialized substring check with a bounded traversal of original strings, including dictionary keys. The traversal rejects actual NUL, excessive depth, excessive item count and cycles before serialization; ordinary JSON/Unicode serialization validation remains in place.

Independent disposition: **resolved**. The reviewer inspected the corrected traversal and tests. Store regressions preserve the literal escape through creation, update, nested metadata keys/values and message bodies. Actual-NUL tests verify rejection without changing task state/history. The HTTP regression verifies literal preservation and actual-NUL rejection against PostgreSQL. Depth and cyclic Python input tests exercise the new traversal bounds.

### Final combined result and evidence

**Pass.** No required findings remain unresolved in the final combined snapshot. The additional review covers the two-line API wiring, fixed asset routes and security headers, browser token lifecycle, safe text rendering, same-origin authenticated requests, revision conflict handling, and the corrected dependency submission path. No further required defect was found.

The reviewer independently reran all six test files with the two disposable database DSNs configured: **114 passed, 1 warning in 18.19 seconds**. The lead separately reported the final combined run as **114 passed in 17.90 seconds**, with the same upstream TestClient warning. The lead reported successful wheel and source-distribution builds and verified that the wheel includes migrations 001/002 and all three web assets. No implementation changes followed those final checks.

The lead's real Chromium/PostgreSQL browser QA covered task creation/edit/history, exact dependency whitespace, stale HTTP 409 disabling save until explicit refresh, injected HTML rendered as text without execution, absent browser storage/cookies, logout clearing private DOM, and desktop/mobile layouts without overflow or page errors. The reviewer independently inspected the supplied desktop/mobile screenshots. Browser interaction results are lead-supplied evidence; the reviewer's independent execution evidence is the 114-test run and source inspection. The screenshots are transient local artifacts, not deployment evidence.

Final acceptance includes the thin workbench against authorized disposable bootstrap tasks. It does not accept live ledger authority, a production deployment, Tailscale/device/browser reachability, production role separation, structural task operations, automatic workflow execution, worker/fleet launches, inference, billing changes or Git publication. Those boundaries remain as described above. The final manifest supersedes the bootstrap-only manifest for integration of this combined candidate; changing any relevant source or governing contract invalidates affected acceptance.

## Final accepted combined SHA-256 manifest

```text
6dc62b0e7d135b949d66c97b99c12155a4ffa60d822b994b9a5d109b1ebe9af1  src/skybuild/__init__.py
3c9a4462d25472e126bb54587ff4f9ff43cc3e03d2cbe2ece059406aee90b401  src/skybuild/__main__.py
5da7fb879cf434d07c1b092ea096fbe7f2e3b7c797cf6b58451b67ba92a0ca4a  src/skybuild/api.py
0b376ab630a71c7f930e2b67b0fdcadc99a69d4f2bfb07ce3bf68a4ad4c8fd0e  src/skybuild/client.py
8124412e09d5a403e9fce82212b2a9bb2e87ac6a6b21a76f977011f18adf93ae  src/skybuild/contracts.py
bb076ffc4da1a30fd608424e6507e3d3eebbfc7168abcfd597cda6c23657cc52  src/skybuild/ledger.py
c14c4d5fbd4218e7378c68554794d2de1bd614d5cc064dd08449d54550438262  src/skybuild/store.py
c31cfa0177833df78050bf33253b655f694a2ec013aaf8fefbae89c69612a64b  src/skybuild/web.py
75b653aba28c7279e4b4b08ca186ddd193c1af086d5e320c6aa09258ca42d9ae  src/skybuild/migrations/001_bootstrap.sql
1b4040897e99a1102f841848000a30f9cf95b7f37a4de0d4a859569be8fd9906  src/skybuild/migrations/002_cord_journal.sql
5e5ab683704e526c6569903eb13049a3986a024c3268df7ec02e9ef5fd0d02e8  src/skybuild/static/workbench.html
ce9df5fa7774d943df2f8d692f0c621e43c617012b7a9ea51238539962c3f6e1  src/skybuild/static/workbench.js
a150d6821186d1a951bd340854f783ea6d42a1ddc87944bd2917a8887c6f9647  src/skybuild/static/workbench.css
6e2082bf77c67568aeb6489c3526456379b0ed4faf19a732cd823447d6e3b584  tests/test_api.py
5342a31da9b000e076ac5071c2ad32d8fda24725f35183d9858300c588df8887  tests/test_store.py
b3776ccc2cf9155d970e3f54f67e4de4c19778e3f00ddcae4a303e6e595e0b86  tests/test_client.py
548ab65741dbd5f49c3faf4181f5656bc87978dc31b9f0ee3679b23f87e398a4  tests/test_ledger.py
aef301438798f45ca389286f9da5bc07c36bfeb2928c4d276c58b7ecbf4be4da  tests/test_bootstrap_integration.py
00c164c328c1d7d7ca92276bf53e51bb22162e2672f55945db459d6528db648d  tests/test_web.py
f2384aa9239dbb698ccca4028321f038d719878f8177e87ff070738d95f9360b  pyproject.toml
6c5bda99bbd7ba7f0a5d501d7385e583573d8b68b73943c58ea0093524765e0d  uv.lock
dccd30d1c1bc0d6c0b951c6ede9f1aa06795f1e1db5565e4dd7aac40e3082339  docs/design/architecture.md
2a3e453bf6197babbd5f5490987f87ad8886a5efcda492f438b940f4c9dfb0cc  docs/design/review_policy.md
e6aab21c300cfc2c7fe4437932bcbad4f3d55d9a865465a37e69eaf7a0b6941a  docs/design/implementation/bootstrap.md
```
