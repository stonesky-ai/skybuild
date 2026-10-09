---
name: skybuild-host-watch
description: Run one CPU-only SkyBuild host watcher per box during sustained work, reporting memory, disk, owned Docker containers and worktree hygiene without model polling.
---

# SkyBuild host watch

Use `scripts/host_watch.py` from the exact checkout on each participating SkyBuild box. One `--watch` process samples memory and disk headroom, task-owned disposable Docker containers and prunable Git worktree records every 60 seconds, then atomically replaces a JSON state file. It makes no network or model calls. Its lock refuses a second watcher for the same file, and its maximum lifetime defaults to eight hours.

Use a writable scratch path outside the checkout, such as `/home/kevin/my_code/skybuild-gate-tmp/host-watch.json`, with `--reserve-gib 4 --disk-reserve-gib 4`. Start one watcher for sustained work, then read the state file only before a resource-heavy command or when diagnosing pressure. Treat `low`, `unknown`, `stopped`, or a sample older than two intervals as a reason to defer new heavy work. `attention` reports owned Docker containers older than the gate deadline or prunable worktrees for manual reconciliation. Allow room above the 4 GiB memory reserve for the command's own demand; the disposable PostgreSQL gate accepts `--min-available-gib 6` for this session.

Commit and propagate the script and skill with SkyBuild before relying on them on another box. Verify each box's checkout head and run the local copy there; each Codex session must read this skill itself. The watch reports pressure and ownership hints; it does not stop processes, remove containers or worktrees, or guarantee future free memory. Never use broad process-kill commands to manage it. Keep the managed terminal session handle and stop the watcher through that handle at closeout. If launched outside a managed session, stop only its verified process; the PID in the state may be namespace-relative. The watcher writes `stopped` on normal termination. Leave unrelated sessions and containers alone.
