---
name: skybuild-disposable-postgres
description: Run SkyBuild's full test suite against three task-owned disposable PostgreSQL databases, then remove the container and report compact results.
---

# Disposable PostgreSQL gate

Trigger for a SkyBuild candidate requiring Store, HTTP and importer database tests. Read `scripts/disposable_pg_gate.py --help`, then run it with `--checkout` set to the exact candidate checkout. The script verifies SkyBuild identity, creates isolated databases, saves the full log in the configured temporary directory, and removes its container even on failure. Report its JSON result and log path. Never point this gate at a live database or treat it as authority cutover evidence.

On a host that must retain 4 GiB of available memory, pass `--min-available-gib 6` to leave room for the test command above that reserve. The PostgreSQL container has a 512 MiB memory cap. Set `TMPDIR` and `UV_CACHE_DIR` to writable task-owned scratch paths when the sandbox cannot write the default `uv` cache. Verify the gate's `cleaned_up` result and that no task-owned container remains.
