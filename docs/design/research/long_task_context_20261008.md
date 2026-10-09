# Long-task context and process monitoring

Checked 2026-10-08 for planning only. No global configuration, running session or external process was changed. Public documentation establishes possible settings, not installed-version qualification or measured savings.

## Engine mechanisms

- **Codex:** official OpenAI documentation defines `model_auto_compact_token_limit` as the automatic history compaction threshold in tokens; an unset value uses model defaults. It also documents threshold scope. Qualify the selected engine/version and task profile rather than assuming a `/autocompact` slash command. Do not substitute a paid Responses API call for subscription-backed CLI compaction. [Configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).
- **Claude Code:** the documented `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` lowers the automatic compaction threshold in supported sessions. `CLAUDE_CODE_AUTO_COMPACT_WINDOW` sets the context capacity used for its calculation and overrides `/autocompact`, `--autocompact` and the corresponding setting. The documented window range is 100,000–1,000,000 tokens, capped by the model; percentage and window are different controls. Confirm the installed version, effective window and supported session mode before choosing numeric settings. [Environment variables](https://code.claude.com/docs/en/env-vars).
- **restart-me:** no matching skill was found in the checked Codex, Agents, Claude user skill directories or SkyKeep/SkyBuild skill/command directories. This is not proof it exists nowhere on the machine. A future adapter can use a verified skill with an appropriate checkpoint contract; native engine compaction is the alternative presently documented. No skill was created or installed.

## Design consequence

Every long-running task carries an explicit model/task-specific numeric ceiling and compaction or checkpoint/restart policy. Select the smallest adequate working packet with headroom; the appropriate number remains an execution-profile decision, not one fixed threshold for every model. Keep restart/compaction exposure in the existing task/interval budget.

A smaller context alone still incurs costs if resent every timer tick. CPU observers own waits, process identity, logs and bounded delta/cursor summaries. Model wakeups require a decision or changed evidence needing reasoning; unchanged status remains available through cached APIs. Retain raw artifacts outside prompts and retrieve specific excerpts only when needed.

Before session restart, checkpoint exact sources, effects, stable external job IDs, cursors, remaining authority and reserved/uncertain exposure. Revalidate those references and controls after restart; reattach to the existing job rather than launching it again. Interrupted compaction/checkpoint, stale revisions, missing mechanisms and repeated restart failure need bounded recovery and visible parked state. No separate memory microservice is proposed.
