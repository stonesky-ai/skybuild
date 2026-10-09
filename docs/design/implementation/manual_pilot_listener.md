# One armed manual pilot receive

This bounded helper belongs to `SKYBUILD-MANUAL-WORKER-PILOT`, based on published commit `7d40df9fa7b26035736ffa613b5c5dad548269f5`. It is an explicitly invoked CPU-only listener, not a service, worker launcher, task queue, or task-authority change. The Markdown ledgers remain authoritative. No automatic startup is installed.

Run the reviewed script with the same project interpreter and checkout-local package used for the other manual pilot commands. Supply the approved private controller URL, worker token file and installation public CA file. Existing private-API preflight verifies TLS, Tailscale addresses, exact worker identity and narrow grants. The token and CA remain file references; do not pass token contents or transfer CA private keys. Use the same private state directory for every listener on that project/worker; its advisory lock refuses concurrent cooperating listeners using that directory.

```sh
PYTHONPATH=/absolute/reviewed-listener-checkout/src /absolute/project-python \
  /absolute/reviewed-listener-checkout/scripts/manual_pilot_listener.py \
  --url https://approved-controller.ts.net:8443 --project skybuild \
  --worker wonko --dispatcher pilot_dispatcher \
  --token-file /private/worker-token --ca-file /approved/public-ca.pem \
  --checkout /absolute/clean-pinned-checkout --base-sha <full-commit-sha> \
  --brief-path docs/design/assignments/<committed-brief>.json \
  --brief-sha256 <exact-committed-brief-digest> --assignment-id <expected-assignment-id> \
  --destination /private/received-assignment.json --state-dir /private/listener-state \
  --approved-until 2026-10-09T15:20:53Z --duration 600
```

The operator must supply an explicit future UTC `--approved-until`. The current assignment is authorized only through `2026-10-09T15:20:53Z`; use that exact cutoff for this assignment. The reusable script does not grant or extend approval. Duration is 1–600 seconds, includes preflight allowance, and uses a monotonic bound so clock rollback cannot extend waiting. Expired or invalid bounds refuse arming. Future authorization requires a separately approved operator invocation; a message cannot renew it. Individual inbox waits are at most 25 seconds. Unrelated pending messages cause a bounded one-second backoff because a Cord receipt does not remove messages from the inbox. Only the first 100 rows are inspected; an expected message beyond that page requires dispatcher reconciliation, not automatic pagination or handling.

The listener selects the one expected assignment ID, then requires the exact armed base SHA and committed brief path/digest. Wrong sender, recipient, category, schema, brief, duplicate expected deliveries, or conflicting destination evidence fail without a receipt. Other IDs and unreadable unrelated bodies are ignored without acknowledgment. The existing assignment validator checks the committed brief and every envelope field. The checkout must be clean at the pinned head when arming and before receipt.

The selected authenticated inbox snapshot is passed to `manual_cord.receive_assignment`, preserving its existing verification, mode-0600 save, file/directory fsync, and deterministic receipt key. Deadline checks precede receipt submission. If the deadline arrives during validation/persistence, the saved file remains for reconciliation and no new receipt is submitted. An already submitted network request may have an uncertain outcome; the listener does not claim remote cancellation or exactly-once delivery. Transport/preflight calls are bounded by existing client timeouts and can finish after the waiting cutoff. After a lost receipt response, rerun the same pins and destination within existing authorization; the same private file is verified/fsynced and the same receipt key is reused.

After successful save and receipt, the listener exits and prints only sanitized status, assignment ID and message ID. Timeout exits 2; a blocked operation exits 1 with a fixed message. It never marks the message handled, mutates a task, executes message text, invokes Codex, starts a model or unit, or launches a process. Existing validation may execute read-only Git commands. The dispatcher separately reconciles receipt and authorizes any next worker action.

Offline tests inject the client and clocks and use committed fixture briefs. They cover immediate arrival, bounded waits, unrelated-message backoff, invalid/foreign messages, clean/pinned checkout requirements, exclusive arming, save-before-receipt, lost-response retry, deadline expiry during persistence, absolute cutoff, and monotonic expiry after clock rollback. No live HTTP, model, service or unit operation is used for these tests.
