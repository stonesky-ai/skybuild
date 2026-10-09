# One-shot manual Cord dispatch

`python -m skybuild.manual_dispatch` sends one pinned assignment after the manual pilot's private-access and send/receive/acknowledge checks. It does not start a worker, change a task, or grant model spending. The Markdown ledgers remain task authority.

For a new dispatch, the brief must be committed on the published `dev-002` head and fetched into `refs/remotes/origin/dev-002` locally. The dispatcher reads the brief bytes from that exact commit, computes their SHA-256 digest, builds the `manual-work-v1` envelope, and runs the existing worker-side assignment validator. It checks the published head again immediately before the first send. A retry reads the pinned durable intent before consulting the current development head, so a lost response can be recovered after that head moves. The worker may then reject the old base during worktree preparation; reconcile that assignment explicitly.

Use a private, owner-only token file and a private state directory outside the checkout. The dispatcher requires a Tailscale HTTPS name resolving only to Tailscale addresses, readiness, an exact non-admin principal, one project grant, and exactly the `cord:send`, `cord:read`, and `cord:handle` scopes. The full HTTPS endpoint, including port, is pinned in the intent. Inspect the controller's Serve/Funnel configuration separately before dispatch: DNS and TLS checks cannot prove the service is not publicly exposed. Do not place credentials in arguments, Git, or Cord bodies.

Example after the controller and both boxes pass the manual pilot preflight:

```sh
python -m skybuild.manual_dispatch \
  --checkout /home/kevin/my_code/skybuild \
  --brief-path docs/design/assignments/<committed-brief>.json \
  --worker wonko --dispatcher pilot_dispatcher \
  --project skybuild --principal pilot_dispatcher \
  --url https://<controller-tailnet-name>.ts.net \
  --token-file /home/kevin/<private-token-file> \
  --state-dir /home/kevin/<private-dispatch-state>
```

Before network send, the command writes and fsyncs a local intent containing the exact Cord body, principal, project, endpoint, and deterministic idempotency key. It marks the intent `sending` before the HTTP call. A retry for the same project and brief path reuses the pinned body and key, even if `dev-002` has moved. The API retains idempotency responses for the same principal, project, operation, key, and payload. A changed brief at the same path does not create a new dispatch; use a new brief path after reconciling the old assignment. Preserve the local state directory and dispatcher principal across retries. A successful call stores the returned message ID atomically. A lost response leaves `sending` state; retry the same command. Do not reassign the task or change principal to work around a failed send.

The worker must still authenticate the Cord sender, save the received envelope, run `skybuild.manual_assignment`, and prepare its owned worktree. The dispatcher must check ledger ownership and existing assignments before first dispatch. This helper does not fence independent dispatchers, prove a recipient's session is running, or replace result reconciliation and independent review.

The brief dispatcher, `--dispatcher`, `--principal`, and authenticated Cord sender must be the same principal. The pilot uses `pilot_dispatcher`, with worker principals `wonko` and `wowbagger`; an SSH hostname such as `wowbaggers` is separate from the Cord identity. Assignment and result messages use category `manual-work`, while their JSON schema remains `manual-work-v1`. Old durable intents with a different category or identity require explicit reconciliation; the command refuses to rewrite or resend changed intent.
