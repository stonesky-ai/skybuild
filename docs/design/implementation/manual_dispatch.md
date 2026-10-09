# One-shot manual Cord dispatch

`python -m skybuild.manual_dispatch` sends one pinned assignment after the manual pilot's private-access and send/receive/acknowledge checks. It does not start a worker, change a task, or grant model spending. The Markdown ledgers remain task authority.

The brief must be committed on the published `dev-002` head and fetched into `refs/remotes/origin/dev-002` locally. The dispatcher reads the brief bytes from that exact commit, computes their SHA-256 digest, builds the `manual-work-v1` envelope, and runs the existing worker-side assignment validator before sending. It checks the published head again immediately before the send. A later branch movement does not revoke a message already sent; reconcile that assignment explicitly.

Use a private, owner-only token file and a private state directory outside the checkout. The dispatcher requires a Tailscale HTTPS name resolving only to Tailscale addresses, readiness, an exact non-admin principal, one project grant, and `cord:send`. Inspect the controller's Serve/Funnel configuration separately before dispatch: DNS and TLS checks cannot prove the service is not publicly exposed. Do not place credentials in arguments, Git, or Cord bodies.

Example after the controller and both boxes pass the manual pilot preflight:

```sh
python -m skybuild.manual_dispatch \
  --checkout /home/kevin/my_code/skybuild \
  --brief-path docs/design/assignments/<committed-brief>.json \
  --worker wonko --dispatcher jeltz \
  --project skybuild --principal <dispatcher-principal> \
  --url https://<controller-tailnet-name>.ts.net \
  --token-file /home/kevin/<private-token-file> \
  --state-dir /home/kevin/<private-dispatch-state>
```

Before network send, the command writes and fsyncs a local intent containing the exact Cord body, principal, project, controller host, and deterministic idempotency key. A retry with the same brief reuses that key and body. The API retains idempotency responses for the same principal, project, operation, key, and payload. A changed brief under the same assignment ID blocks and needs manual reconciliation. Preserve the local state directory and dispatcher principal across retries. A successful call stores the returned message ID atomically. A lost response leaves `prepared` state; retry the same command. Do not reassign the task or change principal to work around a failed send.

The worker must still authenticate the Cord sender, save the received envelope, run `skybuild.manual_assignment`, and prepare its owned worktree. The dispatcher must check ledger ownership and existing assignments before first dispatch. This helper does not fence independent dispatchers, prove a recipient's session is running, or replace result reconciliation and independent review.
