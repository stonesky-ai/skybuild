# Task definition editor

The launch-free workbench now exposes acceptance criteria and architecture references alongside the existing title, brief, next action, responsible owner and dependencies. Both new fields use JSON arrays so each item can retain embedded newlines and exact spacing. The browser rejects malformed arrays before PATCH; the Store remains authoritative for item limits, workflow invalidation, revision checks and Markdown import write protection.

An unrelated edit round-trips a multiline criterion and reference unchanged in the browser-handler regression. Independent `structure_ui_review` found the initially lossy line-based fields and accepted the JSON-array correction. The full disposable PostgreSQL suite passed 134 tests with the existing Starlette TestClient deprecation warning. This editor does not mark acceptance met, assert review evidence or complete a task. It only changes a definition through the existing guarded task update.
