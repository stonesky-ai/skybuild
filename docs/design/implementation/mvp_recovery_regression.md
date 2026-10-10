# MVP recovery Store regression

This task adds one disposable PostgreSQL Store scenario for a failed validation attempt, explicit rework, and a later fenced claim. It binds the failure result and submission receipts to their task inputs, checks that old attempt, fence, and input-generation receipts are refused, and verifies that replays do not append duplicate task or claim history. Source branch is `task/mvp-recovery-regression-20261010`, based on `2629fe60f15fd389f5bd53b5076eb1a1a79feb6d`.

The regression uses existing task and actor fixtures. It changes no production workflow code and starts no worker. A passing test would establish this Store recovery path for the tested database schema and fixture; it would not establish two concurrent feature workers, shared live resource or budget enforcement, controller replacement, real interruption recovery, or deployment acceptance.

Checks run from this checkout:

- `scripts/project_python` import and `py_compile` check passed; `skybuild.__file__` resolved to this checkout's `src/skybuild/__init__.py`.
- `env -u SKYBUILD_TEST_DSN -u SKYBUILD_HTTP_TEST_DSN -u SKYBUILD_IMPORT_TEST_DSN scripts/project_python -m pytest -q tests/test_mvp_recovery_store.py` collected the scenario and skipped its database fixture because no disposable DSN was supplied.
- `git diff --check` passed.

The skipped scenario is not a test pass. Run it against an authorized task-owned disposable PostgreSQL database with the repository disposable PostgreSQL gate. Database execution remains pending shared host-watch admission for this task branch. Independent exact-head review and the full combined gate also remain pending. This isolated Store regression does not prove the live two-worker MVP, shared live budget enforcement, controller replacement, real interruption recovery, or deployment acceptance.
