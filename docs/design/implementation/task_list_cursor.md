# Task-list cursor browsing

The task API now offers an ID-ordered browsing mode: `GET .../tasks?by_id=true&limit=100` followed by `GET .../tasks?after_task_id=LAST_ID&limit=100`. Stable task IDs form the cursor, so changing task priority does not shift existing tasks between pages. The default priority/offset listing remains available for existing clients. Cursor mode refuses a nonzero offset. A task created behind an already visited cursor belongs to the next full scan; it cannot retroactively appear in a completed page.

The workbench uses ID browsing with First/Next controls and keeps requests bounded to 100 records. A full page enables Next; a short page ends the scan. The browser handler, Store and HTTP regressions cover paging, exact cursor IDs and priority changes. Browsing changes no task authority.

If exactly 100 tasks fill the final page, an empty Next response leaves that page visible and disables Next. Independent `structure_ui_review` accepted the final cursor and browser behavior. The full disposable PostgreSQL suite passed 146 tests with the existing Starlette TestClient deprecation warning.
