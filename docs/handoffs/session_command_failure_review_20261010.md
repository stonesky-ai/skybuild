# Session command failure review, 2026-10-10

Reviewed the designated Codex session log read-only. This record contains categories and corrected practices only; it excludes prompts, credentials, and raw log excerpts.

## Findings

- Path discovery failed when commands guessed a README, skill, parent `AGENTS.md`, or retired skill filename. The checkout's token-efficient coding skill already requires listing actual files before opening remembered paths. Follow that rule and check each required read before dependent work.
- GitHub CLI rejected an assumed `baseRefOid` JSON field. Confirm fields in the installed `gh pr view --help`; read `base.sha` from the pull request REST resource or resolve an exact remote ref with `git ls-remote`.
- A new task rejected a direct `ready` transition because creation starts in proposed triage. Create first, read returned workflow state, then use the permitted guarded transition. Do not treat rejection as proof of readiness.
- `Client.update_task` rejected the guessed `revision` keyword. Inspect the selected client's signature and route/model; this client takes `expected_revision`.
- A guessed `Client.renew_claim` method raised `AttributeError` during signature inspection. Inspect available client methods first; this client exposes `claim_task`, while renewal is named elsewhere.
- An imported legacy task lacked a `petri` member. Inspect task kind and returned workflow shape before indexing domain-specific fields. Missing Petri state does not imply readiness.
- Local service startup encountered an existing fixed-name container, and HTTPS probes encountered a hostname/trust mismatch. Check exact container ownership and health before reuse; use the certificate hostname and trusted CA for probes.
- During this review, I selected a different checkout because its branch and base looked similar. I reverted only my exact patch there. Verify the assigned top-level path before every edit; branch or base similarity is not ownership evidence.
- A separate checkout setup attempt used a destination directory as `workdir` before it existed. Run clone from an existing parent, then use the new checkout as `workdir`.

Stopped-port probing and Git no-match/ancestry checks that returned exit code 1 were expected negative checks, not command defects. The service and TLS issues were later resolved by startup/probe correction.

## Prevention changes

Tightened `.claude/skills/skybuild-token-efficient-coding/SKILL.md` with exact checkout ownership checks, checkout creation ordering, available-client-method and signature checks, explicit GitHub base-commit retrieval, and safe task-creation/workflow handling. Tightened `.claude/skills/skybuild-session-failures/SKILL.md` with container ownership and certificate-aware probe checks. Existing `.agents/skills/skybuild-session-failures` trigger and `AGENTS.md` already route sustained work through these skills, so no duplicate trigger or new skill was needed.
