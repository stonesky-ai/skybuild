---
name: skybuild-codegraph-checkout
description: Initialize or refresh CodeGraph for a SkyBuild checkout and verify its index points to that checkout before code navigation.
---

# Checkout-local CodeGraph

Trigger on a new SkyBuild worktree, branch switch or large pull before graph queries. Run `scripts/codegraph_checkout.py --checkout <exact checkout>` to initialize or sync its ignored local index and verify the reported project path. Then query the CodeGraph MCP server started by your own session with that same explicit `projectPath`; if unavailable, use local `codegraph explore`. Never point an agent at another checkout's index or another session's server. The script checks local CLI setup; it cannot prove MCP session provenance.
