---
name: skybuild-session-snapshot
description: Use for one bounded, read-only snapshot of a SkyBuild checkout and explicitly selected task, with optional indexed source exploration.
---

# SkyBuild session snapshot

Use `scripts/session_snapshot.py` through `scripts/project_python` to collect checkout facts and one selected task's workflow summary in one command. Read [the operator note](../../../docs/design/implementation/session_snapshot.md) for arguments and output fields.

Pass the exact checkout, project and task ID. Pass token and CA file paths; never read or print credential contents. The helper makes one GET request for that task and emits only bounded workflow fields. It does not list tasks or read task history.

The helper does not inspect source or call CodeGraph by default. Add a caller-supplied `--graph-query` only when source navigation is needed. It checks CodeGraph status against the exact checkout first and skips missing indexes. It never initializes, syncs or fetches an index.

Treat `unavailable` as missing evidence. Snapshot output reports observations; it does not make readiness, permission, deployment or task-completion decisions.
