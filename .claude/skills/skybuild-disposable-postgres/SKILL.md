---
name: skybuild-disposable-postgres
description: Run SkyBuild's full test suite against three task-owned disposable PostgreSQL databases, then remove the container and report compact results.
---

# Disposable PostgreSQL gate

Trigger for a SkyBuild candidate requiring Store, HTTP and importer database tests. Read `scripts/disposable_pg_gate.py --help`, then run it with `--checkout` set to the exact candidate checkout. The script verifies SkyBuild identity, creates isolated databases, saves the full log under `/tmp`, and removes its container even on failure. Report its JSON result and log path. Never point this gate at a live database or treat it as authority cutover evidence.
