---
name: skybuild-session-failures
description: Review recent SkyBuild coding-session tool failures every 30 minutes and turn demonstrated preventable repeats into a narrow guard or skill. Use during sustained autonomous coding, not for ordinary one-off answers.
---

# SkyBuild session failure review

During a sustained coding session, run `python3 scripts/session_failure_scan.py --watch` from the SkyBuild checkout. It uses ripgrep to select recent Codex JSONL logs and emits only failure categories and agent paths; never paste raw logs, credentials or prompts into reports. Inspect its output every 30 minutes while working. Stop the loop when the session ends.

For each candidate, inspect the specific failed tool result, then decide whether it was an expected negative test, an external condition or a preventable process error. Fix the underlying cause before adding general instructions. When a repeatable practice would have prevented it, write or tighten a narrowly triggered skill in `.claude/skills/`, add its `.agents/skills/` trigger and name it in `AGENTS.md`. Validate the skill with `quick_validate.py`. Do not create a new skill for a one-off typo that a code fix fully resolves.

For regression tests, prove the test reaches the intended path: isolate unrelated DNS, service and credential prerequisites; bound calls that could hang; and, when practical, show the pre-fix behavior fails the test. A passing test caused by an earlier unrelated error is not evidence of the fix.
