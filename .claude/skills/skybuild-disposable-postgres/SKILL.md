---
name: skybuild-disposable-postgres
description: Run SkyBuild's full test suite against three task-owned disposable PostgreSQL databases, then remove the container and report compact results.
---

# Disposable PostgreSQL gate

Trigger for a SkyBuild candidate requiring Store, HTTP and importer database tests. Read `scripts/disposable_pg_gate.py --help`, then run it with `--checkout` set to the exact candidate checkout. The script verifies SkyBuild identity, creates isolated databases, saves the full log in the configured temporary directory, and removes its container even on failure. Report its JSON result and log path. Never point this gate at a live database or treat it as authority cutover evidence.

On the owner's current host, which must retain 8 GiB of available memory, pass `--min-available-gib 10` to leave room for the test command above that reserve. The PostgreSQL container has a 512 MiB memory cap. Set `TMPDIR` and `UV_CACHE_DIR` to writable task-owned scratch paths when the sandbox cannot write the default `uv` cache. Verify the gate's `cleaned_up` result and that no task-owned container remains.

When a test needs a specific ledger-authority state, establish and read back that state explicitly. Schema 012 and current API fixtures may already insert an API-owned `ledger_imports` row; `INSERT ... ON CONFLICT DO NOTHING` does not make it Markdown-owned. A denial test must verify its intended authority precondition before exercising the guard. Run this boundary on the composed current-schema candidate, not only an older task branch.
