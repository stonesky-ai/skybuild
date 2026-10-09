---
name: skybuild-codegraph-checkout
description: Initialize or refresh CodeGraph for a SkyBuild checkout and verify its index points to that checkout before code navigation.
---

# Checkout-local CodeGraph

Trigger on a new SkyBuild worktree, branch switch or large pull before graph queries. Run `scripts/codegraph_checkout.py --checkout <exact checkout>` to initialize or sync its ignored local index and verify the reported project path. Use the installed CodeGraph command-line CLI for all graph queries; do not use the MCP server. The CLI takes a project path as a positional argument for `status`, and with `--path` (or `-p`) for `explore`, `query`, `context`, and `node`:

```sh
codegraph status /absolute/path/to/project
codegraph explore --path /absolute/path/to/project "symbol names or question"
```

Do not pass `--project`; the CLI does not support that option. Keep the exact checkout path on every command so a worktree cannot accidentally use another checkout's index. Use `codegraph sync /absolute/path/to/project` after a large pull, branch switch, or merge. If the checkout has no `.codegraph/` index, follow the repository's indexing rule before creating one.
