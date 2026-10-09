# One-shot dispatcher result collection

Task: `SKYBUILD-MANUAL-WORKER-PILOT`. Source base: `42c3256d1dd05af2c1c457d214231cce9f69c066`. Branch: `task/manual-result-collector`. This completes the dispatcher side of the existing manual REST relay while the Markdown ledgers remain authoritative. It does not handle a result message, launch work, change tasks, approve checks, perform review, merge a branch, or promote the controller.

`skybuild.manual_result.receive_result` takes the existing committed assignment envelope and explicit assignment ID, ledger task ID, worker, dispatcher, base SHA, message ID, destination and approval cutoff. It reuses `verify_assignment` with an injected Git runner that applies the remaining deadline to every Git command. Existing assignment callers retain their original runner. The collector requires the non-admin dispatcher identity with exactly the three project Cord grants. It searches up to three pages of 100 inbox messages by default, with an explicit maximum of ten pages, without waiting or polling. It validates the result sender, recipient, category, exact result field contract, report phase, assigned branch and owned paths.

The reported head must be the exact currently advertised `origin` task branch. That commit must already exist in the operator checkout and descend from the pinned assignment base; its actual changed paths must exactly match the report. The collector does not fetch or modify Git. If an object is unavailable or the branch advanced, collection stops for operator reconciliation. The checked head is evidence at the time of inspection; later review must continue to use that exact SHA. A matching result is not independent review or task acceptance.

The collector persists the assignment and result in a private mode-0600 file and fsyncs the file and directory before requesting only a receipt. Existing evidence must match exactly; a conflicting report cannot overwrite it. Receipt identity derives from project and message ID. An accepted-but-lost reply leaves the saved evidence for the same bounded retry. No implicit `handle` request is made. Results in `in-progress`, `blocked`, and `ready-for-review` phases retain their next action; `done` or other acceptance claims are refused.

Run the one-shot command with the existing installation CA and dispatcher credential:

```sh
PYTHONPATH="$PWD/src" .venv/bin/python -m skybuild.manual_result \
  --url "$SKYBUILD_API_URL" --project skybuild \
  --token-file <private-dispatcher-token> --ca-file <installation-ca.pem> \
  --checkout "$PWD" --assignment <saved-assignment.json> \
  --assignment-id <pinned-assignment-id> --task-id <pinned-SKYBUILD-task-id> \
  --worker <pinned-worker> --dispatcher pilot_dispatcher \
  --base-sha <pinned-40-character-base> --message-id <exact-result-message-id> \
  --destination <private-result-file> --approval-until <existing-approval-cutoff-with-offset>
```

Use the existing approved cutoff; this argument cannot renew implementation or inference authority. Duration defaults to 120 seconds and is bounded to 1–120 seconds. Wall-clock approval expiry and a fixed monotonic budget are checked before and after external calls, before saving and before receipt. Production requests disable automatic retries and inherited proxies; each REST call has at most a five-second timeout and each Git command at most ten seconds, further limited by remaining budget. On Linux, a main-thread `ITIMER_REAL` elapsed guard interrupts even an in-flight response making progress or a blocked resolver. It covers the CLI from before DNS through output, and guards direct `receive_result` calls. Nested guards retain the earlier deadline. Non-main-thread calls and an existing unrelated active alarm are refused before collection; the prior handler is restored afterward. This does not provide a remote cancellation guarantee for an already-issued receipt; an interrupted receipt remains uncertain and replayable with the same key. CLI expiry validation precedes DNS and credential reads. The CLI bounds the assignment file to 64 KiB and emits only safe collection metadata or a generic failure, never result bodies, credentials or subprocess diagnostics.

Focused offline validation uses scratch Git and synthetic inbox/receipt responses. It covers durable-before-receipt behavior, accepted-but-lost receipt replay, conflicting saved evidence, forged identities, assignment/head/path mismatch, bounded pagination, expiry after persistence, monotonic expiry despite wall-clock rollback, failed persistence, and secret-safe CLI failures. No live endpoint, token or model call is needed. Independent exact-head review and the parent's combined gate remain required before integration; live exercise and actual result review remain separate.

Validation before commit: `PYTHONPATH=/home/kevin/my_code/skybuild-manual-result-collector/src /home/kevin/my_code/skybuild/.venv/bin/python -m pytest -q tests/test_manual_result.py tests/test_manual_assignment.py tests/test_manual_cord.py tests/test_manual_dispatch.py` passed **54 tests**, with no skips. `git diff --check` passed. The imported source is pinned to this owned checkout through its absolute `src` path.

## Independent review correction R1

Independent review of `31ec58a655d02d2a9afdbca9c134322e51c076db` requested changes: HTTPX read timeouts bound inactivity, so progressing responses and synchronous DNS could exceed the advertised total deadline. The first 54 passing tests did not establish that bound. The separate review is retained at `skybuild-gate-tmp/manual-result-collector-review-31ec58a.md`.

The elapsed guard now covers those blocking boundaries rather than relying on post-call checks. Three new regressions fail against the original reviewed module loaded in an isolated test process; the progressing-body and blocked-resolver tests specifically exceed their allowed elapsed times there. Corrected focused validation passes **58 tests**, no skips, including accepted-but-interrupted receipt replay and fail-closed non-main-thread behavior. No production endpoint or credential was used; the HTTP timing fixture binds only to synthetic loopback. Independent exact-head re-review remains required before integration.
