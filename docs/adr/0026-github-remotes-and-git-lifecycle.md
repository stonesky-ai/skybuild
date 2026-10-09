# ADR 0026: Initial GitHub remotes and scoped Git lifecycle

Date: 2026-10-08. Status: accepted GitHub assumption, GitLab/Bitbucket deferral and task-owned reset/clean plus confirmed post-merge cleanup; lifecycle checks proposed. Implementation: not started.

## Context and accepted direction

All projects presume pushes to their configured GitHub repositories. Include task-owned source/docs and committed checkpoint/handoff artifacts in the normal commit/push workflow. GitLab and Bitbucket support are later work under SKYBUILD-GIT-HOSTS; no generic hosting framework is required initially.

Record repository, branch, exact local commit and confirmed versus pending remote push. Unavailable/failed pushes retain local work and pending status. A task-branch push does not merge a PR, deploy a product, preserve unexported database state or renew execution/model authority. This supersedes the earlier closeout restriction against implicit pushes in ADR 0019.

## Accepted task-owned and post-merge Git policy

The owner confirmed reset/clean inside task-owned worktrees and branch/worktree cleanup after a confirmed PR merge into the intended target/higher-level branch. Protect other workers’ work and shared branches. Build instructions/ADRs must not impose blanket prohibitions on destructive Git commands.

This resolves the earlier ambiguity about allowed cases versus protected exceptions. No additional prompt is required merely because an operation is destructive within existing task authority and this scope. Acceptance is a design decision; no destructive Git operation is performed by this planning task.

## Proposed lifecycle evidence

Keep ownership, the affected worktree/ref, intended remote/base branch and PR outcome explicit. Check both worktree and ref ownership; a worktree-local reset can move its checked-out branch. A closed-but-unmerged PR is not a merge; account for squash/rebase and commits added after merge when confirming retained work. Validate allowed cleanup and cross-worker/shared-ref denial through one shared lifecycle check. Do not report local-only commits as pushed or expand authority to unrelated work or repository/database secrets.

Architecture: sections 2 and 8. Implementation implications: normal GitHub task-branch delivery, checkpoint push status and scoped lifecycle validation; GitLab/Bitbucket adapters are deferred. No push, hosting account query or destructive Git command was run.
