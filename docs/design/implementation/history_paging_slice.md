# Workbench task-history paging

Task detail loads immutable journal events in revision order, 100 per request. “Load more history” appends the next page and disables when the API returns fewer than 100 events. This keeps newer events reachable for long-lived tasks without an unbounded browser request. Refreshing a task starts again at its first page; logout clears the loaded history. The API's per-task revision order is stable because history rows are append-only.

The browser regression exercises a 101-event task and verifies that the second page appends once and then disables the control. Independent `structure_ui_review` accepted the pagination and task-switch/logout behavior. This read-only control does not alter live task authority.
