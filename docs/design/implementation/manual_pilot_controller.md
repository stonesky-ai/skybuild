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

`init-secrets` creates high-entropy administrator/runtime passwords and owner, dispatcher and worker tokens in a private mode-0700 directory. The PostgreSQL password file is mode 0644 because PostgreSQL's container user must read its mounted copy; its host parent directory is mode 0700. Every other secret file is mode 0600. The one-time provisioner refuses a preexisting `skybuild_pilot` database or `skybuild_pilot_runtime` role, verifies the running Compose container, its local image, loopback port, mounted state and PostgreSQL system identity before any mutation, applies checked-in migrations, installs and audits the restricted runtime grants, and creates `pilot_owner`, `pilot_dispatcher`, `wonko` and `wowbagger` principals. `pilot_dispatcher` is non-admin and has only `cord:read`, `cord:send` and `cord:handle` on `skybuild`, so workers can address results to it and it can receive, acknowledge and handle those results. It has no task operations or access to other projects. Workers receive only `tasks:read`, `cord:read`, `cord:send` and `cord:handle` on project `skybuild`. It writes `runtime.env` only after the audit passes. Never put secrets in shell arguments, Git, logs or Cord bodies. A partial setup needs inspection; do not rerun provisioning blindly.

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

The worker check verifies private TLS/DNS, readiness, exact identity, project grants and Cord inbox. Next send a harmless JSON pilot message from `pilot_dispatcher` to each worker using `skybuild cord-send` with `--body-file`, let the worker receive it with `cord-inbox`, acknowledge with `cord-receipt` and `cord-handle`, and send a `manual-result-v1` result back to `pilot_dispatcher`. Verify the dispatcher receives, acknowledges and handles that result. Use a unique idempotency key and record message IDs and exact outcomes. Use `pilot_dispatcher`'s token for the bounded manual assignment dispatch command, whose identity check requires principal `pilot_dispatcher` and exactly `cord:read`, `cord:send` and `cord:handle` on `skybuild`. The brief and assignment dispatcher must match that authenticated principal; worker principals are `wonko` and `wowbagger`. Only then dispatch the two committed, disjoint briefs under [manual worker pilot](manual_worker_pilot.md). The ledgers remain authoritative.

## Optional application TLS when Serve operator access is unavailable

Tailscale Serve remains the default. The owner approved this bounded fallback on 2026-10-09 within the eight-hour window ending 15:20:53 UTC. It does not authorize changing Tailscale operators, ACLs, firewall rules, system trust or sandbox privilege boundaries. Do not use self-SSH or Docker as a privilege bypass. Preserve the healthy dedicated controller while qualifying this option. The only remote listener is the controller's current Tailscale IPv4 address at 8443; workers still use its exact ts.net DNS name, never an IP URL.

First integrate and publish the independently reviewed TLS client/server and deployment changes. Obtain the full reviewed published source SHA from the integration record and verify the clean deployment checkout, the existing Compose project ownership, image identities, restricted database audit and private state location. Do not rerun database provisioning. The following certificate preparation/check command enforces the current local Tailscale DNS/IP identity and known running controller, requires owned mode-0700 state, and starts no service. Use the existing OpenSSL executable; install nothing automatically. Read the fresh host watcher and retain 8 GiB available memory before building or restarting the API.

```sh
APPROVED_SHA='REPLACE_WITH_REVIEWED_PUBLISHED_40_HEX_SHA'
CONTROLLER_HOST='REPLACE_WITH_CURRENT_SELF_DNS_NAME_WITHOUT_TRAILING_DOT'
export SKYBUILD_PILOT_TAILNET_IP='REPLACE_WITH_CURRENT_SELF_TAILSCALE_IPV4'
export SKYBUILD_PILOT_TLS_UID="$(id -u)"
./.venv/bin/python scripts/manual_pilot_tls.py generate --checkout "$PWD" --expected-sha "$APPROVED_SHA" --state-dir "$SKYBUILD_PILOT_STATE" --hostname "$CONTROLLER_HOST" --tailnet-ip "$SKYBUILD_PILOT_TAILNET_IP"
```

Derive hostname and address from `tailscale status --json` Self, rather than copying stale values. Preparation creates a new private `$SKYBUILD_PILOT_STATE/tls` directory with mode-0600 CA key, CA certificate, server key and server certificate. It refuses existing TLS state. The leaf contains exactly the DNS SAN and serverAuth usage, lasts 30 days, and chains to a dedicated one-year CA. The report includes expiry, public CA fingerprint and runtime UID, never key material. `check` in place of `generate` revalidates existing state and refuses certificates with less than one day remaining. Retain the CA key only on this controller; mount only the server certificate/key readonly. Never mount the CA key or transfer it to a worker. The optional overlay runs the API as the non-root numeric TLS-file owner, so the existing image UID 10001 cannot cause a private-key read failure. Verify the reported UID equals the exported UID and the effective container user; never loosen keys to world-readable mode or run this overlay as root.

After preparation, independently inspect the rendered Compose configuration without printing environment secrets: require only the base loopback API binding and the verified Tailscale-IP:8443 binding, the two readonly leaf mounts, the exact non-root runtime UID, and native paired `--ssl-certfile`/`--ssl-keyfile` flags. PostgreSQL stays only on 127.0.0.1:55432 with its existing state. Confirm the Tailnet port is free and current grants allow intended workers; do not widen them automatically. Re-run `check` immediately before applying the reviewed overlay, and use the same two Compose files for every subsequent inspection/restart/rollback operation:

```sh
./.venv/bin/python scripts/manual_pilot_tls.py check --checkout "$PWD" --expected-sha "$APPROVED_SHA" --state-dir "$SKYBUILD_PILOT_STATE" --hostname "$CONTROLLER_HOST" --tailnet-ip "$SKYBUILD_PILOT_TAILNET_IP"
docker compose -f ops/manual-pilot/compose.yaml -f ops/manual-pilot/compose.tls.yaml up -d --build --no-deps api
```

Compose retains `127.0.0.1:8000:8000`, but that port now speaks TLS too. The earlier HTTP readiness probe and an HTTP Serve target no longer apply while this overlay is active. Verify local readiness with pinned CA and the DNS name, resolving that name to loopback only for the local probe; then verify the actual private endpoint from each worker. Do not use `curl -k` or disable client verification. Confirm no Funnel/public bind and verify wrong-CA, wrong-hostname and expired-certificate rejection using isolated test certificates, never by replacing live controller credentials.

```sh
curl --fail --silent --cacert "$SKYBUILD_PILOT_STATE/tls/ca.crt" --resolve "$CONTROLLER_HOST:8000:127.0.0.1" "https://$CONTROLLER_HOST:8000/health/ready"
```

Copy only `tls/ca.crt` through the existing approved SSH channel to an owned worker file and compare its SHA-256 certificate fingerprint with the controller report. Pass `--ca-file <public-ca-file>` to worker preflight and all Cord/manual commands at `https://<current-controller-ts.net>:8443`. Complete exact scoped-principal/grant checks and authenticated send/receive/receipt/handle round trips before assignment dispatch. Optional transport qualification does not launch workers or authorize inference. Record the observed endpoint, reviewed source, CA fingerprint, expiry, bind/user/mount checks and message IDs as deployment evidence, without credentials.

For rollback, stop dispatch and preserve in-flight assignment/result state. Recreate only the API using the base Compose file to restore its former loopback HTTP command and remove the Tailnet binding; verify the actual bindings and readiness. Do not touch the database, state directory or unrelated Serve routes. Keep CA/leaf files for inspection. Rotation is a separate explicit manual operation before expiry, requiring a new reviewed preparation/transfer/qualification sequence; no renewal service or global CA trust is introduced.

## Restart, rollback and limits

Docker's restart policy preserves the dedicated database and restarts both services after Docker/host restart. After a restart, run `docker compose -f ops/manual-pilot/compose.yaml ps`, check `/health/ready` and check Serve reachability; do not infer readiness from container state alone. The initial preflight intentionally fails once the owned ports and container name are in use. The API has no migration or provisioning privileges. Schema upgrades must stop the API, use administrator credentials explicitly, requalify the runtime role, then restart. The bounded [010-to-011 promotion procedure](manual_pilot_promotion.md) adds an exact-identity read-only preflight and an explicit single migration/qualification transaction. It preserves application TLS and documents why the older binary is not rollback-ready after 011 commits.

To stop the pilot, first stop dispatch and reconcile accepted Cord messages. Inspect Serve ownership. If its pre-pilot configuration was empty and no other operator changed it, `tailscale serve reset` removes the pilot route; otherwise reconcile its exact route without clearing unrelated services. Then run `docker compose -f ops/manual-pilot/compose.yaml down`. `down` leaves the bind-mounted database, secret files and committed ledger authority intact. Do not run `down --volumes`, remove another container, delete the state directory, or revoke credentials as an automatic rollback. To resume, run `docker compose -f ops/manual-pilot/compose.yaml up -d` and repeat readiness/private-access checks. If database state or credentials need disposal, document and approve that separate irreversible action.

This pilot has no public endpoint, automatic worker launcher, task API cutover, daily backup or tested restore. Docker restart and PostgreSQL persistence are not a backup. A machine outage leaves the API unavailable; workers retain their authorized current-task outage behavior and must reconcile before reassignment.
