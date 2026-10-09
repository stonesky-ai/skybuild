---
name: skybuild-token-efficient-coding
description: Use for SkyBuild code navigation, edits and validation when CodeGraph, RTK and Serena can reduce source and command-output context.
---

# Token-efficient SkyBuild coding

At session start read the available CodeGraph and RTK skills and Serena's `initial_instructions` if Serena tools exist. Activate the exact SkyBuild checkout in Serena before a symbol query or edit; if Serena does not respond, continue with CodeGraph and direct source tools. Verify `codegraph status` names this checkout before a graph query. Initialize a missing checkout-local index as authorized in `AGENTS.md`; never use another checkout's index or another session's MCP server.

For code questions, use one scoped CodeGraph `explore` query first. Treat its line-numbered source as already read. Use Serena for targeted symbol inspection or edits when that is more precise; do not repeat a full source read. Use `rtk` for shell output and `rtk proxy` when exact bytes matter. Use `rg` for literal text, docs, config and graph gaps. Run `codegraph sync` after a large pull or branch switch.

CodeGraph and Serena are navigation aids, not proof of absence. Confirm zero-caller or other absence claims with direct search before deleting code. Required tests and independent review still apply.

When a task uses another checkout's virtual-environment interpreter, set `PYTHONPATH` to the task checkout's absolute `src` directory before running Python or pytest. An editable installation in the shared environment otherwise imports the other checkout, even when the shell runs in the task directory. Confirm `skybuild.__file__` resolves inside the intended task checkout before recording test evidence. Prefer the disposable gate's checkout-local `uv run` environment for the combined gate. Use the project interpreter for provisioning commands that need installed dependencies; a successful standard-library preflight does not prove system Python can provision the service. Check each prerequisite command's exit status before starting dependent services.
