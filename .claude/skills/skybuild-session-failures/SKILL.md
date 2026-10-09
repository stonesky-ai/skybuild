---
name: skybuild-session-failures
description: Review recent SkyBuild coding-session tool failures every 30 minutes and turn demonstrated preventable repeats into a narrow guard or skill. Use during sustained autonomous coding, not for ordinary one-off answers.
---

# SkyBuild session failure review

During a sustained coding session, run `python3 scripts/session_failure_scan.py` from the SkyBuild checkout for a single bounded review. It uses ripgrep to select recent Codex JSONL logs and emits only failure categories and agent paths; never paste raw logs, credentials or prompts into reports. Inspect recent results every 30 minutes while continuing assigned work and processing coordination messages.

For repeated CPU-only sampling, use `--watch --duration-minutes <1–480>` and pass `--approval-deadline <ISO-8601 timestamp with timezone>` when the session has an approval cutoff. Watch mode requires an explicit duration or deadline, uses the earlier bound, and never runs longer than eight hours. A monotonic cutoff prevents wall-clock rollback from extending that lifetime. An expired deadline performs no scan. Run the watcher in the background with a task-owned output file or managed process handle; record that ownership for cancellation. Never keep the reasoning session in a `write_stdin` or other wait loop until the watcher finishes. Read bounded output only when a review is due, then return to useful work and queued messages. Do not restart a watcher automatically at its cutoff. End only the owned watcher with SIGTERM or SIGINT when the session ends; cancellation interrupts its wait and parsing, with an in-flight ripgrep call bounded by its timeout.

For each candidate, inspect the specific failed tool result, then decide whether it was an expected negative test, an external condition or a preventable process error. Fix the underlying cause before adding general instructions. When a repeatable practice would have prevented it, write or tighten a narrowly triggered skill in `.claude/skills/`, add its `.agents/skills/` trigger and name it in `AGENTS.md`. Validate the skill with `quick_validate.py`. Do not create a new skill for a one-off typo that a code fix fully resolves.

For regression tests, prove the test reaches the intended path: isolate unrelated DNS, service and credential prerequisites; bound calls that could hang; and, when practical, show the pre-fix behavior fails the test. A passing test caused by an earlier unrelated error is not evidence of the fix.
