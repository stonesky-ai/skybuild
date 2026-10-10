# Subtask startup context audit

Task: `SKYBUILD-SUBTASK-STARTUP-TOKENS`. Source baseline: `1e0c2ff03586ff4543ce81abe01303f30265c41c` on `dev-006`. This changes instruction packaging, not architecture, acceptance gates, runtime permissions or model profiles.

## Observations

On 2026-10-10, a CPU-only scan of that day's local Codex JSONL sessions selected 36 records with a dictionary-valued subagent source and an agent path. For each, it read only the first `token_usage_record.payload.usage`; raw transcripts were not exported. These are workspace subtasks, not a controlled SkyBuild-only benchmark. First-request input ranged from 17,101 to 24,631 tokens, with median 21,352. Median input minus cached input was 2,162 tokens. This is first-request context, not total tokens through completion of startup reads. Cache hits do not remove context or establish a monetary cost.

The two required repository entrypoints contained 3,864 tokens under `tiktoken`'s `o200k_base` encoding. After this change they contain 1,474: 2,390 fewer tokens, a 61.9% reduction in those files. `AGENTS.md` changes from 2,245 to 834 tokens; the coding skill changes from 1,619 to 640. This tokenizer is a reproducible proxy, not a claim about the exact tokenizer of every provider. Architecture, machine/user instructions, tool schemas and task briefs are outside this reduction. There is no measured end-to-end child-session improvement yet.

## Changes and retained capabilities

- The root instructions retain universal authority, billing, review, task history and publication constraints. Full previous policy remains in `docs/agent-policy.md`, with explicit operation triggers in the root entrypoint. Specialized policy is mandatory before its corresponding operation.
- The coding skill retains checkout-local navigation, exact reads, project interpreter isolation and patch checks. API, nested candidate, runner and validator details move to its linked `references/operations.md` and remain required when relevant.
- Serena is initialized only for symbol work that needs it. CodeGraph remains first for indexed code navigation; direct tools remain available without an index. Machine instructions control index creation and tool transport.
- Independently authorized subtasks prefer a complete bounded packet with `fork_turns="none"`. Task identity, exact checkout/revision, contract, acceptance, allowed effects, budget/deadline and uncertain effects remain required. Preserve governing instructions not automatically inherited. Use needed history if a reliable packet cannot be formed. This does not authorize new delegation or change model selection.

## Reproduction and limits

Count entrypoint text with `len(tiktoken.get_encoding("o200k_base").encode(text))`, comparing the two paths at the baseline commit against this revision. Use an isolated dependency environment and a task-owned writable cache; do not add a tokenizer to project runtime dependencies. The two `.agents` trigger files still route to the same project skill.

For future qualification, compare first-request input/cache usage and total input/output through the first substantive task action for comparable child tasks. Record packet size, model/profile and whether history was forked. Include correction and review costs through acceptance. Do not compare cached tokens to uncached tokens as if they have the same billing treatment, or treat shorter instructions as proof of unchanged behavior. Required independent review and the existing combined integration gate remain intact.
