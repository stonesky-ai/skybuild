---
name: skybuild-session-failures
description: Audit SkyBuild session transcripts, repeated tool errors or failed exit(2) messages, and review failures every 30 minutes during sustained coding. Identify reusable automation only when repeated use saves tokens.
---

# SkyBuild session failure review

During a sustained coding session, run `rtk proxy scripts/project_python scripts/session_failure_scan.py` from the exact SkyBuild checkout for a single bounded review. It uses ripgrep to select recent Codex JSONL logs and emits only failure categories and agent paths; never paste raw logs, credentials or prompts into reports. Inspect recent results every 30 minutes while continuing assigned work and processing coordination messages.

For requests such as “audit session transcripts,” “repeated tool errors,” or “failed exit(2),” add `--minutes 2880 --details` for the last two days, or select the requested window. This single Python invocation combines log selection, invocation/result pairing, exit counts, fixed diagnostic labels and at most three event locations per label. It never prints raw commands or error text. Labels can overlap and indicate candidates, not established causes; inspect the referenced invocation and result privately before calling a failure preventable. Script errors without a structured exit code remain `script_error`, rather than guessing an exit code from quoted text. Detailed mode is a one-shot audit; do not combine it with watch mode.

Before adding automation, compare its implementation, validation and discovery cost against tokens saved per repeated sequence. Prefer extending an existing helper and trigger. Record the break-even repetition count; skip a new skill when existing tools already cover the sequence or expected use cannot repay its cost.

For repeated CPU-only sampling, use `--watch --duration-minutes <1–480>` and pass `--approval-deadline <ISO-8601 timestamp with timezone>` when the session has an approval cutoff. Watch mode requires an explicit duration or deadline, uses the earlier bound, and never runs longer than eight hours. A monotonic cutoff prevents wall-clock rollback from extending that lifetime. An expired deadline performs no scan. Run the watcher in the background with a task-owned output file or managed process handle; record that ownership for cancellation. Never keep the reasoning session in a `write_stdin` or other wait loop until the watcher finishes. Read bounded output only when a review is due, then return to useful work and queued messages. Do not restart a watcher automatically at its cutoff. End only the owned watcher with SIGTERM or SIGINT when the session ends; cancellation interrupts its wait and parsing, with an in-flight ripgrep call bounded by its timeout.

For each candidate, inspect the specific failed tool result, then decide whether it was an expected negative test, an external condition or a preventable process error. Fix the underlying cause before adding general instructions. When a repeatable practice would have prevented it, write or tighten a narrowly triggered skill in `.claude/skills/`, add its `.agents/skills/` trigger and name it in `AGENTS.md`. Validate the skill with `quick_validate.py`. Do not create a new skill for a one-off typo that a code fix fully resolves.

For local service startup, check the exact fixed container name before `compose up`; identify its owner and health before reusing it, and do not remove an unknown container to clear a name conflict. For HTTPS probes, use the hostname covered by the service certificate and its trusted CA bundle. Do not hide a hostname or trust-chain mismatch with `curl -k`.

For regression tests, prove the test reaches the intended path: isolate unrelated DNS, service and credential prerequisites; bound calls that could hang; and, when practical, show the pre-fix behavior fails the test. A passing test caused by an earlier unrelated error is not evidence of the fix.
