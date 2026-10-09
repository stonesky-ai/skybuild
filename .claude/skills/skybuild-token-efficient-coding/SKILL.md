---
name: skybuild-token-efficient-coding
description: Use for SkyBuild code navigation, edits and validation when CodeGraph, RTK and Serena can reduce source and command-output context.
---

# Token-efficient SkyBuild coding

At session start read the available CodeGraph and RTK skills and Serena's `initial_instructions` if Serena tools exist. Activate the exact SkyBuild checkout in Serena before a symbol query or edit; if Serena does not respond, continue with CodeGraph and direct source tools. Use the CodeGraph command-line CLI, not MCP. Verify `codegraph status /absolute/path/to/checkout` names this checkout before querying. `status` takes a positional path; `explore`, `query`, `context`, and `node` use `--path /absolute/path/to/checkout`. Never pass `--project`. Initialize a missing checkout-local index as authorized in `AGENTS.md`; never use another checkout's index.

For code questions, use one scoped `codegraph explore --path <exact-checkout> "<query>"` query first. Treat its line-numbered source as already read. Use Serena for targeted symbol inspection or edits when that is more precise; do not repeat a full source read. Use `rtk` for shell output and `rtk proxy` when exact bytes matter. `rtk proxy` runs a program; it does not interpret a command string as a shell. Set the command tool's `workdir` instead of writing `cd` into `rtk proxy`, or invoke an explicit shell when shell syntax is required. Use `rg` for literal text, docs, config and graph gaps. Run `codegraph sync <exact-checkout>` after a large pull or branch switch.

Before reading a referenced file, confirm its path exists with `rg --files` or `rtk ls`; do not guess paths from a brief or an earlier checkout. Before using a Python interpreter, confirm that interpreter exists in the exact checkout. A new worktree usually has no ignored `.venv`; in that case use the primary checkout's interpreter with `PYTHONPATH` set to the task worktree's absolute `src`, then verify `skybuild.__file__` points into that worktree. Use a short heredoc or a task-owned script for multi-line Python. Avoid deeply nested shell/Python quoting and escaped quotes inside f-string expressions; use `.format()` or separate script files.

After applying a patch or creating a file, verify the target exists and `git status` or `git diff` shows the expected change before relying on it; a tool response alone does not prove the write succeeded.

CodeGraph and Serena are navigation aids, not proof of absence. Confirm zero-caller or other absence claims with direct search before deleting code. Required tests and independent review still apply.

When a task uses another checkout's virtual-environment interpreter, set `PYTHONPATH` to the task checkout's absolute `src` directory before running Python or pytest. An editable installation in the shared environment otherwise imports the other checkout, even when the shell runs in the task directory. Confirm `skybuild.__file__` resolves inside the intended task checkout before recording test evidence. Prefer the disposable gate's checkout-local `uv run` environment for the combined gate. Use the project interpreter for provisioning commands that need installed dependencies; a successful standard-library preflight does not prove system Python can provision the service. Check each prerequisite command's exit status before starting dependent services.
