---
name: skybuild-token-efficient-coding
description: Use for SkyBuild code navigation, edits and validation with bounded context and checkout-local tools.
---

# Token-efficient SkyBuild coding

Read instructions once per session. Read only the working contract at the start of `docs/design/architecture.md` and governing sections; there is no separate `working_contract.md`. Discover uncertain filenames before opening them. Keep required reads checked so later success cannot hide a failed prerequisite.

- Prefix shell commands with `rtk`; use `rtk proxy` for exact bytes, test counts and pre-edit source. Set `workdir` explicitly. `rtk proxy` executes a program, not a shell string. Check every exit code; stop dependent work on failure.
- Use this session's CodeGraph for this checkout only, as machine instructions direct. Verify `codegraph status <exact-checkout>` before the first query. Without an index, use direct tools unless the owner directs indexing. With an index, make one scoped `explore` query before code searches/reads; its source is already read. Sync after large pulls or switches. Confirm absence independently before deletion.
- Defer Serena initialization until precise symbol work needs it. Activate the exact checkout first. Bound optional calls to 30 seconds; on timeout use available direct tools. Never initialize every tool merely because it is installed.
- Use `scripts/project_python` for project commands, including help. It selects the checkout's locked environment and source. Use task-owned writable `UV_CACHE_DIR` when needed. Verify source imports before accepting evidence from another interpreter or candidate.
- Prefer bounded patches. Verify writes with `git diff`/status. Assert match counts for scripted replacements. Use a heredoc or owned script for complex quoting. Filter output at source; never dump raw sessions or full process arguments.

Before the following operations, load the matching paragraphs in [operations](references/operations.md): nested candidate validation or reused interpreters; runner `safe_path` imports; unfamiliar Git/GitHub/client arguments; API task creation/transitions; skill validation. Those detailed safeguards remain mandatory when applicable. Other guarded workflows use their own matching project skill. Required tests and independent review still apply.

## Bounded subtasks

For an authorized independent subtask, prefer `fork_turns="none"` with a self-contained packet: task ID, exact checkout and source revision, scope/contract and relevant document anchors, deliverables/acceptance, applicable checks, allowed effects, budget/deadline, known completed or uncertain effects, and return format. Include unresolved findings for re-review. Preserve all applicable user/machine instructions in the packet when they are not automatically supplied. If these inputs cannot be reconstructed reliably, retain the needed history instead of dropping it. Do not change model/profile or permissions to save tokens.

Do not paste parent transcripts, entire architecture, task snapshots or full skill catalogs. Reference exact artifacts; the child reads only governing sections and relevant source. Cached input still consumes context; measure cached and uncached usage separately. This guidance does not authorize delegation, model starts or new work. CPU discovery, waits and log counts need no reasoning subtask.
