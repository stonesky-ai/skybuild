---
name: skybuild-branch-closeout
description: Close a completed SkyBuild development branch after its standing PR merges to main, then safely remove merged branches and verify only main remains.
---

# Branch closeout

Trigger when the owner declares a development cycle complete or requests no open PRs and only `main`. Follow SkyKeep's `close-pull-request` and `branch-pr-merge` guidance only where SkyBuild has matching machinery. Before branch deletion, confirm the standing PR's exact head/base, finish review and combined gates, post a substantive feature and limitation comment, and merge it to `main` without squash.

Run `scripts/closeout_branches.py --checkout <SkyBuild root> --target main` to inspect the plan. After checking its expected main SHA and ensuring no worktrees or open PRs remain, run with `--apply --expected-target <full SHA>` under the owner's closeout authorization. The script refuses unmerged refs and uses leases on remote deletions. It does not tag, release, create a successor branch or repoint SkyKeep boxes.
