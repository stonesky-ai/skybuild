---
name: ste100
description: Rewrite SkyBuild documents, descriptions, briefs and agent instructions in compact STE-inspired English. Triggers include ste100, /ste100, $ste100, STE100 rewrite, compact STE, and Simplified Technical English. Preserve meaning and authority; not for code transformations or creative copy.
license: MIT
---

# Compact STE for SkyBuild

Use this skill for the requested text or artifact. It does not set a permanent chat style or authorize unrelated edits.

Rewrite using STE-inspired English. Minimize tokens while preserving every requirement, fact, condition, exception, uncertainty, identifier and authority boundary. Remove repetition. Preserve technical terms. Return only rewritten text. Flag any compression that could change meaning.

## Rewrite

1. Read the source for meaning. Identify its facts, actors, requirements, conditions, exceptions and uncertainty before shortening it.
2. Use active voice, plain verbs, consistent terms and complete grammar. Keep articles, subjects and negation. Prefer simple tenses when they preserve meaning. Put a condition before its command. Keep one instruction per sentence and one topic per paragraph. Aim for at most 20 words per instruction and 25 per descriptive sentence; preserve precision when a sentence needs more.
3. Remove filler, repeated explanations and unnecessary nominalizations. Replace idioms with literal language. Split dense noun clusters and long sentences. Use lists for real steps or parallel items. Do not remove unique facts to meet a length target.
4. Compare the rewrite with the source clause by clause. Check every number, unit, identifier, condition, exception, actor, requirement and uncertainty. Restore any lost meaning. If the source is already compact and clear, return it unchanged.

## Preserve these distinctions

- Keep `must`, `should`, `may`, `can`, prohibitions and approval conditions at their original strength. Never turn a recommendation into a requirement, permission into capability, or possibility into fact. Keep meaningful hedges and compound tenses such as “may have failed.”
- Keep source code, commands, flags, paths, URLs, hashes, schema fields, task IDs, quoted errors and exact quotations unchanged. Rewrite surrounding prose only. Do not rename project terms for stylistic variety: tasks, attempts, processes and bundles are distinct.
- Preserve scope, ownership, dependencies, source revisions, evidence provenance/freshness, budgets, deadlines with time zones, stop conditions, uncertain external effects and API/task authority. Do not equate reviewed, tested, pushed, integrated, accepted or deployed. A rewrite grants no authority, weakens no gate and cannot correct task state.
- Do not invent causes, facts, measurements or approvals. Preserve unresolved contradictions rather than silently choosing a version. Keep a necessary longer phrase if shortening it risks ambiguity. Do not claim formal ASD-STE100 compliance without checking the official standard and vocabulary.

## Output

For a rewrite, return the rewritten text without an introduction or recap. Add a short `Kept as-is:` note only when needed to explain an unresolved ambiguity or unsafe compression. If the user requests a check, diff or explanation, provide that instead. For an authorized file edit, apply the rewrite to the requested file and report changed paths and validation; the output-only rule does not suppress required work reports or approval boundaries.

This skill adapts [asd-ste100-skill](NOTICE.md). It needs no network call, model subtask or linter at invocation time. Structural checks alone cannot prove semantic equivalence.
