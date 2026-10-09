# ADR 0016: Bound long-task context and keep process monitoring on CPU

Date: 2026-10-08. Status: accepted owner requirement for compaction/restart on every long-running task; engine qualification and task-specific numeric settings remain proposed. Implementation: not started.

## Context and decision

The owner requires an appropriate `/autocompact` threshold or `restart-me` skill for every long-running task. Repeatedly sending large context to monitor a process wastes limited model allowance.

Each long-running task declares a numeric context ceiling and supported automatic compaction threshold or verified checkpoint/restart policy before unattended admission. Select settings for the actual model, task class and engine version, with room for the brief, bounded evidence, output and checkpoint. A pure CPU job does not need an LLM monitor.

CPU observers retain logs and process state, emit bounded changes with read cursors/artifact references and coalesce unchanged progress. Wake a model for changed evidence needing reasoning, an actionable exception or a decision boundary. Elapsed time alone cannot trigger a repeated no-change model prompt.

## Consequences and proposed mechanics

Reuse Runner/Keeper checkpoints and Observer artifacts. Preserve exact source/brief/worktree hashes, completed effects, stable job IDs, cursors, next decision and remaining authority/budgets. After compaction/restart, revalidate current controls, billing, source revisions and held exposure. Reference evidence rather than replaying entire transcripts/logs.

Restarting the reasoning session cannot relaunch an existing external job, erase an uncertain effect, reset usage, renew approval or clear a stop. Fence/reconcile the previous agent and reattach only by verified job identity. Bound compaction/restart costs and retry count within the original task and interval reservations; park unchanged restart loops.

The checked local skill directories contain no restart-me skill. Do not claim it is enabled or invent a common slash command. Native documented compaction provides an alternative to qualify; no global settings, running sessions or external jobs were changed during planning.

## Alternatives

Compaction alone does not prevent repeated model polling. Relying on near-full context defaults leaves token savings and checkpoint headroom unplanned. Adding a separate memory service is unnecessary for the initial design.

Architecture: sections 9 and 14. Implementation implications: execution-profile context policy, CPU event coalescing, bounded checkpoint/restart and reattachment checks in area 5. [Context-policy research](../design/research/long_task_context_20261008.md) records supported settings and remaining qualification.
