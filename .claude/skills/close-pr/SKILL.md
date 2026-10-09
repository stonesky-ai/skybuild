---
name: close-pr
description: Safely close a SkyBuild pull request. For a standing development-cycle closeout, finish the merge and start the next cycle in this same workflow. Trigger on “Close-PR”, “close PR”, “close out a PR”, “close a development branch/cycle”, or “done and start-next”.
---

# Close-PR

Invoke for “Close-PR”, “close PR”, “close out a PR”, “close a development branch/cycle”, or “done and start-next”. Read `AGENTS.md`, this skill, and the relevant project review/integration skill before acting. Work only in the SkyBuild checkout and verify its root and `origin` URLs before any GitHub or Git write. Do not modify SkyKeep.

## Establish the requested closeout

Identify the exact PR by number and head branch, never by title alone. For a development-cycle close, confirm the owner's stated boundary and intended base; reaching the approximate 500-commit point is a scheduling signal, not approval to merge or delete refs. Record the PR number, head/base branch names and SHAs, current state, mergeability, and requested outcome. If the PR or boundary is ambiguous, stop before writes and ask one focused question.

Fetch current refs and inspect `git status`, open PRs, worktrees, dependent PRs, and the branch inventory. Preserve unrelated dirty or untracked work. Confirm the exact head is the reviewed candidate, required independent review and checks cover that head/base, and the standing development PR's required full gate has passed at cycle closeout. Require GitHub to report the PR open, clean, and mergeable; investigate behind/ahead changes or stale evidence instead of relying on a prior snapshot.

Before merge, publish a concise, substantive PR comment describing delivered features, limitations, validation evidence, and known-bad or unresolved state. Use `scripts/pr_text.py comment --pr <number> --file <UTF-8 file> --checkout <SkyBuild root>` for multiline comments; read `.claude/skills/skybuild-pr-text/SKILL.md`. A comment is additive review evidence; do not silently replace the PR body. Only post when the owner's request and existing repository authority cover that write.

## Merge and verify

Use `.claude/skills/skybuild-reviewed-pr-integration/SKILL.md` and `scripts/integrate_reviewed_pr.py` for reviewed task PR integration. Supply the exact PR number, base, expected head/base SHAs, retained review evidence, and the required gate. Its default mode validates only; `--merge` is allowed only when the owner has authorized integration under current policy. Check the helper's documented behavior and limitations first.

For the standing `dev-NNN` to `main` PR, finish the required full cycle gate and merge with a merge commit (`gh pr merge <number> --merge`) when authorized. Do not squash or rebase the standing PR. Never bypass failed or stale checks, required reviews, or a ref race. If GitHub or a helper reports an uncertain outcome, inspect PR state and exact refs before retrying; do not create duplicate comments or merges.

After merge, verify the PR's merged state, intended base, merge commit, and resulting remote target SHA/tree. Confirm the target contains the reviewed candidate and that no unexpected commit appeared. Record the exact evidence in the final report. Merging a PR alone does not prove deployment or release.

## Optional branch cleanup

Only clean up branches when the owner requested closeout and the intended merge is verified. Inspect branch and worktree ownership first; do not remove another worker's active or unmerged work. For the repository-wide closeout that leaves only `main`, use `scripts/closeout_branches.py --checkout <SkyBuild root> --target main` in dry-run mode and review its complete proposed deletion list. The script requires a clean checkout on `main`, no other worktrees, no open PRs, and evidence that every remaining branch is merged. Its apply mode deletes every non-target local and remote branch, so do not use it if any listed ref must remain.

Apply only with explicit owner authorization for cleanup and the exact target SHA from the reviewed dry-run: `scripts/closeout_branches.py --checkout <SkyBuild root> --target main --apply --expected-target <full SHA>`. Reconcile any blocked or partial result by inspecting current refs; never weaken the guard or use a broad force-delete. Verify final refs and open PR state. These SkyBuild procedures do not create release tags, rotate branches, or change host/worker configuration unless separately documented and authorized.

## Start the next development cycle

This is the mandatory final phase for a standing `dev-NNN` → `main` cycle closeout. Do not run it when merging an individual task or bundle PR into a development branch. Keep close and next-cycle startup in this single Close-PR workflow; do not create a separate start-next skill.

After the old cycle's merge and target verification, create the next sequential `dev-NNN` branch from the exact merged `main` SHA. Make its first commit contain exactly one new README line that records the new cycle start. Run `git diff --check`, push the branch, and open its standing PR to `main` with title `dev-NNN → main: [summary]`. Use the GitHub-assigned PR number. Leave the new PR open for the next cycle; its required full gate runs at that cycle's closeout. Report both the completed merge and the new branch/PR.

## Report

Report the PR number, exact head/base and merge commit, the feature/limitation comment, relevant gate and review evidence, final target SHA, cleanup performed, and any remaining blockers. Clearly distinguish merged, published, released, and deployed states.
