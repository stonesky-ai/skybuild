# Bounded due-deferral catch-up timer

This slice implements explicit automatic CPU catch-up through the existing authenticated `tasks/reconcile-due` endpoint. It adds no migrations or startup side effects. The CLI command `schedule-due PROJECT` catches up immediately, then repeats after a bounded delay for a finite number of ticks. Defaults are 60 ticks, 60 seconds between ticks, 20 pages per tick and 100 tasks per page. Bounds are validated before any API call.

Each incomplete sweep retains its stable task-ID cursor across ticks. A completed sweep resets to the beginning, so tasks created behind a cursor are reached during the following sweep. Restart starts a fresh sweep; no historical timer ticks are replayed. Store revision checks and resume transitions preserve append-only history under overlapping callers. This is not worker ownership or admission.

An unconfirmed or malformed API page stops the timer, retaining the last confirmed cursor in flushed JSON. It never skips the uncertain page. The existing HTTP client bounds transport retries and uses a stable idempotency key within each request. Later retry/restart can safely rescan because accepted resume transitions change the task state. Authorization and Markdown authority failures stop the timer through the same failure path. No supervisor, cron entry or live service is installed.

Sources at base `99027448800b7e97ebdbcec10e15bbcfaf6b7f6e`:

- Architecture A33, working contract and section 13 compact CPU topology; SHA-256 `dccd30d1c1bc0d6c0b951c6ede9f1aa06795f1e1db5565e4dd7aac40e3082339`.
- Task workflow, timezone/downtime deferral and immutable reassessment contracts; SHA-256 `fedc22b2fc7777879d15c3bc156166b29094ef118dd933c72a0f17292acbada6`.

Validation covers immediate catch-up, bounded delay/ticks/pages, continuation across ticks, completed-sweep restart, failed-page retention, malformed responses and repeated cursors, invalid bounds and CLI wiring. Existing client and API checks remain applicable. Independent review is pending.
