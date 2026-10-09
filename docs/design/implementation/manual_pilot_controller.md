# Dedicated controller for the manual REST/Cord pilot

Task: `SKYBUILD-MANUAL-WORKER-PILOT`. Source base: `77ede37b83515c3b461279116da213e99059c43b`. This is an operator procedure for a small, persistent controller on Jeltz. It does not switch task authority from the Git ledgers, start workers, or connect to any SkyKeep database. Live database provisioning, credentials, and Tailscale Serve changes require the existing deployment approval.

## Topology and preflight

Docker Compose runs a dedicated `postgres:16` database and a SkyBuild API container. Its database data lives in an operator-owned directory outside Git. Docker publishes PostgreSQL only on `127.0.0.1:55432` and the API only on `127.0.0.1:8000`. Tailscale Serve adds private HTTPS after the local service and scope checks pass. The database container has 768 MiB and the API 512 MiB memory limits, PID limits and `unless-stopped` restart policies. The API has only the restricted runtime DSN; migrations and credential provisioning are explicit administrator steps.

Run from a clean SkyBuild checkout at the exact commit approved for deployment and published at `origin/dev-002` (or `origin/main` after promotion). Obtain that 40-character SHA from the reviewed integration record; do not derive it from the local checkout. Set an absolute, new state directory outside the checkout; do not use a SkyKeep path:

```sh
export SKYBUILD_PILOT_STATE=/home/kevin/my_code/skybuild-pilot-state
APPROVED_SHA='REPLACE_WITH_APPROVED_40_HEX_SHA'
./.venv/bin/python scripts/manual_pilot_controller.py --checkout "$PWD" --expected-sha "$APPROVED_SHA" --published-ref refs/heads/dev-002
```

The preflight is read-only. It requires the exact published SHA and a clean checkout, 10 GiB available memory, leaving room above the owner's 8 GiB reserve, 4 GiB disk headroom, free loopback ports, local `postgres:16`, a free pilot container name, a running Tailscale client and an empty Serve configuration. A failed or unknown check stops setup. Inspect current Tailscale grants for the intended devices separately; local status cannot prove access policy. The image build context uses a root `.dockerignore` allowlist that sends only `pyproject.toml`, `src/`, the pilot Dockerfile and pinned requirements; ignored credentials and local checkout state are excluded.

## Explicit setup after approval

Use these commands in order. They are scoped to the pilot Compose project. Do not substitute the SkyKeep database or its credentials.

```sh
./.venv/bin/python scripts/manual_pilot_provision.py init-secrets --state-dir "$SKYBUILD_PILOT_STATE"
docker compose -f ops/manual-pilot/compose.yaml up -d db
docker compose -f ops/manual-pilot/compose.yaml ps
./.venv/bin/python scripts/manual_pilot_provision.py provision --state-dir "$SKYBUILD_PILOT_STATE"
docker compose -f ops/manual-pilot/compose.yaml up -d --build api
curl --fail --silent http://127.0.0.1:8000/health/ready
```

`init-secrets` creates high-entropy administrator/runtime passwords and owner, dispatcher and worker tokens in a private mode-0700 directory. The PostgreSQL password file is mode 0644 because PostgreSQL's container user must read its mounted copy; its host parent directory is mode 0700. Every other secret file is mode 0600. The one-time provisioner refuses a preexisting `skybuild_pilot` database or `skybuild_pilot_runtime` role, verifies the running Compose container, its local image, loopback port, mounted state and PostgreSQL system identity before any mutation, applies checked-in migrations, installs and audits the restricted runtime grants, and creates `pilot_owner`, `pilot_dispatcher`, `wonko` and `wowbagger` principals. `pilot_dispatcher` is non-admin and has only `cord:send` on `skybuild`. Workers receive only `tasks:read`, `cord:read`, `cord:send` and `cord:handle` on project `skybuild`. It writes `runtime.env` only after the audit passes. Never put secrets in shell arguments, Git, logs or Cord bodies. A partial setup needs inspection; do not rerun provisioning blindly.

Verify the API container receives only `SKYBUILD_DSN` and `SKYBUILD_EXPECTED_DATABASE`. Check `/health/ready` and authenticated `/api/v1/me` locally. Then, after checking tailnet grants and confirming Serve is still empty, run:

```sh
tailscale serve --bg 8000
tailscale serve status --json
```

Confirm Serve points only to the loopback API and Funnel is disabled. The expected private endpoint on the current Jeltz tailnet is `https://jeltz.tail991ac1.ts.net`; derive the current name from `tailscale status --json` before use. Keep PostgreSQL local; do not publish port 55432 through Tailscale.

Copy each worker's token file through the approved SSH channel to a user-owned mode-0600 file on that worker. On each box, run:

```sh
python -m skybuild.fleet_preflight --url https://jeltz.tail991ac1.ts.net --project skybuild --token-file <private-worker-token-path> --principal <wonko-or-wowbagger>
```

The worker check verifies private TLS/DNS, readiness, exact identity, project grants and Cord inbox. Next send a harmless JSON pilot message from `pilot_owner` to each worker using `skybuild cord-send` with `--body-file`, let the worker receive it with `cord-inbox`, acknowledge with `cord-receipt` and `cord-handle`, and verify the owner sees the result. Use a unique idempotency key and record message IDs and exact outcomes. Use `pilot_dispatcher`'s token for the bounded manual assignment dispatch command, whose identity check requires exactly `cord:send`. Only then dispatch the two committed, disjoint briefs under [manual worker pilot](manual_worker_pilot.md). The ledgers remain authoritative.

## Restart, rollback and limits

Docker's restart policy preserves the dedicated database and restarts both services after Docker/host restart. After a restart, run `docker compose -f ops/manual-pilot/compose.yaml ps`, check `/health/ready` and check Serve reachability; do not infer readiness from container state alone. The initial preflight intentionally fails once the owned ports and container name are in use. The API has no migration or provisioning privileges. Schema upgrades must stop the API, use administrator credentials explicitly, requalify the runtime role, then restart.

To stop the pilot, first stop dispatch and reconcile accepted Cord messages. Inspect Serve ownership. If its pre-pilot configuration was empty and no other operator changed it, `tailscale serve reset` removes the pilot route; otherwise reconcile its exact route without clearing unrelated services. Then run `docker compose -f ops/manual-pilot/compose.yaml down`. `down` leaves the bind-mounted database, secret files and committed ledger authority intact. Do not run `down --volumes`, remove another container, delete the state directory, or revoke credentials as an automatic rollback. To resume, run `docker compose -f ops/manual-pilot/compose.yaml up -d` and repeat readiness/private-access checks. If database state or credentials need disposal, document and approve that separate irreversible action.

This pilot has no public endpoint, automatic worker launcher, task API cutover, daily backup or tested restore. Docker restart and PostgreSQL persistence are not a backup. A machine outage leaves the API unavailable; workers retain their authorized current-task outage behavior and must reconcile before reassignment.
