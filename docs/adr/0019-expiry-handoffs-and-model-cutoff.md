# ADR 0019: Expiry handoffs, bounded closeout grace and model-query cutoff

Date: 2026-10-08. Status: accepted owner decisions for expiry closeout, short configurable grace, model-query cutoff, independent CPU result reporting and owner-question visibility. The five-minute configurable default was subsequently confirmed below; adapter mechanics and finite configuration bounds remain to be qualified. Implementation: not started.

## Context and decision

Proactive drain reduces unfinished tasks but cannot guarantee none remain. The owner selected compact/commit/handoff at expiry. A short configurable grace is sufficient for closeout; if it is insufficient, hard-stop model queries. The owner explicitly clarified that this does not stop an independently running Python/CPU application. Such applications must report results without a model watching them.

At approval expiry, stop productive model work, compact task context, commit owned work in progress and write a durable handoff-summary attached to the task. Grace permits only this closeout, not continued model-assisted implementation or new tasks. Its deadline is fixed from approval expiry and cannot restart on reconnect or session restart. Reserve closeout usage beforehand; hard usage limits, subscription-only billing and owner stops still bind.

At grace expiry, fence/cancel model queries from every managed path, including nested tools and auto-resuming engines. Report uncertain in-flight provider activity honestly. Independently authorized CPU jobs retain their own resource/runtime/no-progress limits and report progress, terminal results and artifacts through the existing API/Cord/observer and durable spool. No LLM polling loop is needed; later reasoning waits for renewed eligibility.

## Handoff and owner interview

The resume summary identifies exact task/source/commit/worktree, completed work, unfinished steps, test evidence, live jobs, artifact references, next action, current authority and held exposure. Assess barriers to fast completion. If an owner answer could enable fast completion, include a concise question, options/recommendation and the likely benefit/confidence.

The control panel has a task-linked owner-interview section for these questions, distinct from deferred dashboard polish. Persist authenticated answers and supersede stale questions. An answer supplies a decision; it does not renew approval or enlarge a budget.

Before API cutover, attach a task-linked Markdown handoff. Afterwards reuse existing artifact metadata and task history/latest-resume reference. A WIP commit is incomplete evidence, not task completion or deployment authorization. Commit only owned changes; do not stage unrelated edits, include secrets or implicitly merge/push.

## Failure handling and proposed mechanics

Journal exact state before compaction and quiesce agent writes. Keep model transport and CPU job supervision separate. CPU jobs use pinned inputs and isolated outputs so their progress does not invalidate the source checkpoint. Git/artifact/API publication is not atomic: track prepared, committed and attached state, preserve local evidence and replay attachment idempotently. Commit failure retains a patch/untracked inventory; unavailable API retains an attachment-pending spool entry.

At model cutoff or unavailable allowance, save a deterministic summary from retained structured progress and flag missing assessment. Bounded CPU commit/spool/result reporting may finish under its own authority and timeouts; never keep a model querying to watch it. Missing or failed closeout remains visible and recoverable, with claims/exposure retained until reconciliation.

## Alternatives and scope

Later owner clarification, 2026-10-08: default closeout grace to five minutes, configurable in the control panel. Capture the configured value with the approval/policy version. Its fixed deadline remains approval expiry plus that grace; reconnect, profile refresh or restart cannot move it. This time is for compact/commit/handoff only and never enlarges a hard usage cap. Architecture revision A23 records the default and implementation implications.

An open-ended finish policy or continued implementation during grace was not selected. A blanket process kill would discard useful CPU work. Assuming a long-running app needs a model watcher is a design failure; require a reporting path when that app is created.

Architecture: sections 5, 8–9 and 14. Implementation implications: closeout/cutoff enforcement, owned WIP commits, task-linked checkpoint recovery, CPU result contracts and the basic owner-interview panel in execution-control adoption. This resolves residual-task expiry behavior from [ADR 0009](0009-timed-approval-and-daily-provider-caps.md) and [ADR 0018](0018-pre-limit-worker-drain.md), while preserving the separate contact-loss policy in [ADR 0007](0007-outage-policy-and-deferred-failover.md).

Later owner direction: [ADR 0026](0026-github-remotes-and-git-lifecycle.md) makes task-owned GitHub branch pushes part of normal delivery. It supersedes the earlier restriction on implicit pushes; recording a push does not authorize merging/promoting the checkpoint. Offline or failed push remains pending, with local evidence retained.
