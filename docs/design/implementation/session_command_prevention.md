# Session command failure audit

Task: `SKYBUILD-SESSION-COMMAND-PREVENTION`.

The owner requested inspection of failed commands and durable prevention rules on
2026-10-10. The active session's tool results were inspected; the aggregate
three-hour scan included other sessions and was not treated as an error count.
No raw logs or credentials are retained here.

Verified failures and corrections:

- File reads guessed historical `workbench/web.py`, an implementation note and
  root `compose.yaml`. Current files were discovered as
  `ops/runtime-ui/runtime_ui.py`, `ops/runtime-ui/README.md` and
  `ops/runtime-ui/compose.yaml` and read successfully.
- A Git inspection ran outside a repository. Subsequent inspections use explicit
  `git -C` or checkout `workdir`.
- Workspace-wide discovery entered PostgreSQL data and hit permission errors.
  Discovery is now restricted to repository code/docs and owned coordination
  directories; no permissions were changed.
- Absolute-path filtering matched the checkout's `workbench` name and emitted
  unrelated test files. Checkout-relative searches identified the actual tests.
- A task PATCH included managed metadata from GET and was rejected. The corrected
  writable-field payload omitted reserved metadata and succeeded at revision 2.
- Scope mutation with a held claim was rejected earlier. Ownership was released
  before amendment, followed by reassessment and a new fenced claim.
- A `ready` action on an already-ready task was rejected. Owner reassessment
  restored current readiness; GET then confirmed `claim` enabled at revision 3.
  No second ready POST was made. API model validation alone does not establish
  action eligibility; Store and pure workflow guards must also be inspected.
- Remote development advanced while local tracking remained stale. A live remote
  SHA and GitHub tree inspection confirmed the actual target before reporting
  cleaner absence; PR 79 had merged during the investigation.

An earlier unavailable service and refused connection were external conditions;
later authenticated listing succeeded. Expected negative ancestry/search checks,
agent wait timeouts and exception strings in source are not implementation bugs.

The existing token-efficient coding and failure-review skills now contain the
specific guards. Their existing `.agents` triggers and AGENTS references remain
valid. This documentation does not change task authority, runtime configuration
or integration requirements. Skill validation, exact-head review and development
inclusion are recorded separately.
