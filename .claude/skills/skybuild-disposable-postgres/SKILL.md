---
name: skybuild-disposable-postgres
description: Run SkyBuild's full test suite against three task-owned disposable PostgreSQL databases, then remove the container and report compact results.
---

# Disposable PostgreSQL gate

Trigger for a SkyBuild candidate requiring Store, HTTP and importer database tests. Read `scripts/disposable_pg_gate.py --help`, then run it with `--checkout` set to the exact candidate checkout. The script verifies SkyBuild identity, creates isolated databases, saves the full log in the configured temporary directory, and removes its container even on failure. Report its JSON result and log path. Never point this gate at a live database or treat it as authority cutover evidence.

On the owner's current host, pass `--min-available-gib 6` for the owner-directed gate exception. Keep writable `TMPDIR` and `UV_CACHE_DIR` paths when the sandbox cannot write the defaults. PostgreSQL has a 512 MiB memory cap. Verify `cleaned_up` and confirm no task-owned container remains.

For the full suite, set `TMPDIR` to a task-owned private directory beneath `/tmp`, created with `mktemp -d /tmp/skybuild-gate-XXXXXXXX`. Dunsel tests deliberately reject state beneath group-writable, non-sticky ancestors; a workspace scratch directory can therefore be writable yet unsuitable as the test temporary root. Keep the production path guard intact and do not change shared workspace permissions to make tests pass. `UV_CACHE_DIR` may remain in writable workspace scratch. Preserve the gate log and receipt before removing an owned temporary directory.

When a test needs a specific ledger-authority state, establish and read back that state explicitly. Schema 012 and current API fixtures may already insert an API-owned `ledger_imports` row; `INSERT ... ON CONFLICT DO NOTHING` does not make it Markdown-owned. A denial test must verify its intended authority precondition before exercising the guard. Run this boundary on the composed current-schema candidate, not only an older task branch.
