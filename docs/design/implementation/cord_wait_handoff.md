# SKYBUILD-CORD-WAIT implementation handoff

Source base: `dd83f131d26572199e674e810e595552c9b8a27a` (frozen REST pilot bundle). Owned branch: `task/mailbox-bounded-wait`. Governing architecture: A36, sections 6 and 10; the architecture and implementation plan include the bounded polling contract in this revision.

The inbox API accepts optional `wait_seconds` from 0 through 25 whole seconds. Existing callers remain immediate. An empty page waits with half-second asynchronous sleeps; every Store read runs in the thread pool, and repeated checks reauthenticate the bearer token and authorize current project grants. No database transaction stays open across sleeps. A pending page returns without receipt or handling changes; timeout returns an empty list and disconnect stops waiting. Store/database operation timeouts remain independent of the requested waiting interval.

The Python client accepts the same parameter, extends only its per-request read timeout by five seconds beyond the requested wait, and preserves other configured timeouts and the existing bounded retry policy. Transport failures can therefore extend the total caller duration across the configured number of attempts. The one-shot command `skybuild cord-inbox PROJECT --wait-seconds 25 --limit 1` prints the first pending message or an empty list, then exits. It does not listen indefinitely, execute a message, start a model, claim an assignment or acknowledge delivery. Any repeated CPU listener is a separately supervised caller; durable Cord remains authoritative.

Local focused validation uses the existing interpreter with an explicit task-checkout source path:

```sh
rtk proxy env PYTHONPATH=/home/kevin/my_code/skybuild-mailbox-wait/src nice -n 10 /home/kevin/my_code/skybuild/.venv/bin/python -m pytest tests/test_cord_wait.py tests/test_api.py tests/test_client.py tests/test_bootstrap_integration.py -q
```

Result: 73 passed, 26 skipped because no disposable HTTP database DSN was supplied. The new non-database cases cover arrival, timeout, immediate reads, credential and grant revocation, initial denial, malformed waits, disconnect, responsiveness during synchronous database work, client timeout isolation and one-shot CLI behavior. Six added PostgreSQL cases cover committed arrival, token rotation and grant revocation with migration-owner and restricted-runtime connections. The parent runs those in its disposable database combined gate and arranges independent exact-head review before integration. No deployment, endpoint credential changes, service start or live task-authority mutation is part of this branch.

Adjacent manual Cord, fleet-preflight and manual-relay checks: 23 passed, 2 database cases skipped. `git diff --check` passes.

The owner's next priority is an early REST-worker Brodson qualification spike once the workers are operating, using a stronger already approved subscription model to lead or review bounded serial coding/review/latency tests. Architecture and plan record this sequencing. The spike retains canary, zero-charge and shared-capacity constraints and does not imply stress testing, infrastructure changes, paid fallback or early endpoint calls. Full shared-inference runtime prerequisites remain unchanged.
