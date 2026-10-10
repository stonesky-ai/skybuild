# Running SkyBuild Workbench composition

This bounded gateway combines the latest retained Workbench layout with the
existing authoritative schema-013 REST API. It does not connect to PostgreSQL,
provision credentials, import legacy tasks, or launch workers.

Task: `SKYBUILD-LIVE-WORKBENCH-STACK`. The gateway draft was prepared before
this task was claimed; the task journal records the actual later claim.

## Start the existing installation

From this checkout, run:

```sh
rtk proxy scripts/start_runtime_stack
```

This installation-specific command verifies the retained database container,
immutable image, data and password-file mounts, PostgreSQL environment and
cluster identifier. It starts that same database only if stopped, waits for its
health check, then starts the pinned API image without building or migrating.
The API must pass a bounded installation-CA and hostname-verified readiness
request on port 8000 before Workbench starts. A second verified readiness request
on port 8443 must pass before the command reports success. Failure leaves existing
services available for inspection; it never recreates the database or rolls back
task data. Repeating the command reconciles only the API and Workbench services.

The retained installation state is
`/home/kevin/my_code/skybuild-pilot-state`. `SKYBUILD_PILOT_STATE` may select
that same qualified mount through an equivalent canonical path; it cannot select
another database. The Workbench runtime image contains the reviewed gateway
source and uses the API source embedded in its pinned base image; Compose mounts
no application source. Moving the installation requires a separate reviewed
configuration.

Open `https://jeltz.tail991ac1.ts.net:8443/workbench` with the installation CA
trusted. The certificate names this private hostname. Workbench also publishes
port 8443 on loopback; verified loopback probes retain the certificate hostname
with `curl --resolve`. The API publishes port 8000 only on loopback. Workbench
publishes on loopback and the explicit installation Tailscale interface.

Default gateway mode uses an existing authorized project token entered in the
browser. Starting the stack does not start a worker, dispatcher, inference
process, or usage collector. Those processes retain their own task, credential,
budget and lifecycle controls.

## Private Workbench login

Private MVP mode requires a username and password before it grants access to the
configured project. Enable it with explicit server-side credential files:

```sh
--workbench-token-file /run/secrets/skybuild-workbench-token \
--workbench-project skybuild \
--workbench-username user1 \
--workbench-password-file /run/secrets/skybuild-workbench-password
```

Mount the selected API token and password as read-only regular files owned by the
Workbench process user with mode `0600`. Set password contents outside Git; never
place the password in Compose, an environment variable, HTML, an asset, a response,
or a log. The gateway rejects symlinks, group/world-readable files, other owners,
malformed values, or missing options. Do not mount or auto-select the pilot
owner-token path. The browser receives the configured project ID only.

Login creates a random server-side session. Its cookie is Secure, HttpOnly,
SameSite=Strict and expires after eight hours. The server stores only a hash of
each session ID in memory; restart invalidates every session. Five failed logins
from one client address within one minute return HTTP 429. Login, logout, task
pages and tokenless task API requests require the session. Logout revokes the
session immediately. Private mode requires TLS, including on loopback.

Private mode injects the server token only for the configured project's task CRUD,
task-action, history, lineage, task workflow, and workflow-board routes. It denies
Cord, claim, reconcile, split/merge, other projects and unrelated API routes when
the caller is using the tokenless browser path. Existing API clients and workers
may continue to send their own bearer credential through the gateway; those
requests bypass the private Workbench allowlist and never receive the configured
server token. A request cannot combine caller Authorization with the private
Workbench intent header. Every tokenless private API fetch requires
`X-Skybuild-Workbench: 1`; every mutation also requires an exact
same-origin `Origin` and same-origin Fetch Metadata when the browser supplies it.
Revision and idempotency headers remain unchanged. The
gateway redacts the configured credential if an upstream response echoes it.
The existing unauthenticated `/health/live` and `/health/ready` checks remain
available without credential injection.

This is a temporary private installation adapter, not a change to direct REST API
authentication, worker/fleet credentials, or gateway defaults. Keep the existing
loopback/Tailscale listeners and certificate checks. Do not deploy an unreviewed
candidate.

## Composition

The frozen UI comes from `9e72026dce285c99085f0180d90b13144641b2c2`.
The authoritative workflow UI comes from
`7da0ef6542f2a9aeae6feac529eca63b0b2d5bca`. `asset-pins.json` identifies
each copied input; startup refuses changed assets. The task list retains the
latest layout but disables fabricated fallback rows. A valid empty API list
remains empty. Private browser sessions never receive the API bearer token.

Routes:

- `/workbench/login`: private username/password sign-in.
- `/workbench` and `/workbench/tasks`: current task layout backed by REST.
- `/workbench/workflow`: authoritative Petri workflow board and transitions.
- `/workbench/session`: create or revoke the browser session.
- `/api/v1/*` and `/health/*`: fixed-upstream REST proxy.
- `/workbench/runtime`: non-secret composition identity.
- Unwired status, milestone, process-control, and sample-data routes return 503
  with an explicit unavailable message. They never present sample status as live.

## Immutable gateway image

`Dockerfile` copies this gateway, its pinned UI inputs, and its TLS client into a
new local image. Its base is the already accepted local API image ID
`sha256:77bc01222c9c018d44968198e26d0be4618825b35ba07bb8c993002bce4f51c5`.
Verify that exact local base first. Build without pulling and capture the output
image ID:

```sh
docker build --pull=false --iidfile /tmp/skybuild-workbench-image-id \
  --file ops/runtime-ui/Dockerfile .
```

Set `SKYBUILD_WORKBENCH_IMAGE` to the captured `sha256:<image-id>` in the private
Compose environment. Compose has no build rule and uses `pull_policy: never`;
it cannot silently replace the reviewed local image. The runtime mounts only
the installation CA/certificate/key plus the separate API-token and password
files. It does not mount source code or task data. Image build does not start
the Workbench service or connect to PostgreSQL.

The base image keeps its Python package tree private to UID 10001, while the
Workbench runs as UID 1000/GID 0. The image grants GID 0 and other users search
permission on `/app/src/skybuild`, then read/search permission on the public
`/app/src/skybuild/static` snapshot used for startup pin verification. It leaves
the package contents and other source directories unchanged.

Run inside the existing pilot Docker network using dependencies from the pinned
base image. The image includes the runtime at `/runtime-ui`; mount the
installation TLS directory at `/tls` and the two protected credentials under
`/run/secrets`.

```sh
python /runtime-ui/runtime_ui.py \
  --ui-checkout /runtime-ui/ui \
  --api-checkout /app \
  --ca-file /tls/ca.crt \
  --backend-hostname jeltz.tail991ac1.ts.net \
  --backend-connect-host api --backend-port 8000 \
  --workbench-token-file /run/secrets/skybuild-workbench-token \
  --workbench-project skybuild --workbench-username user1 \
  --workbench-password-file /run/secrets/skybuild-workbench-password \
  --host 0.0.0.0 --container-listener --port 8443 \
  --ssl-certfile /tls/server.crt --ssl-keyfile /tls/server.key
```

Container wildcard binding requires the explicit `--container-listener` flag.
Publish only the approved host interfaces. The API remains TLS protected on its
existing internal port. The gateway connects to the fixed Docker service name,
verifies the installation CA and the explicit certificate hostname, and ignores
ambient proxy and CA environment settings. Redirects are not followed.

Private Workbench mode binds only to loopback or an explicit Tailscale address.
It permits a wildcard bind only with `--container-listener`, for the reviewed
Compose deployment whose host port mappings stay on approved interfaces. It
rejects public IP and DNS listeners. Default caller-token mode keeps its existing
listener behavior.

In default mode, the gateway forwards caller bearer authentication, If-Match, and
Idempotency-Key unchanged. It drops cookies and forwarded-host headers. Cross-origin
mutations are refused. Requests and responses have bounded sizes. Upstream errors
return a fixed message, and Uvicorn access logging is disabled.

Focused checks:

```sh
rtk proxy scripts/project_python -m pytest \
  tests/test_runtime_ui_gateway.py tests/test_runtime_ui_private_mode.py \
  tests/test_runtime_stack_startup.py -q
```

Result: 49 passed. These checks cover routes, empty lists, installation trust,
SNI, fixed destination, caller and server credential boundaries, task CRUD/history/
workflow loading, mutation intent, request limits, unavailable panels, origin/Host
checks, redirects, sanitized failures, qualified database checks, startup ordering,
changed-asset refusal, oversized upstream responses, refusal after readiness failure,
private listener boundaries, synchronized login throttling, Dockerfile/Compose
argv composition, build-context allowlisting, and executed Node VM DOM tests for
both generated private page scripts. Those tests verify automatic task/workflow-
board reads, rendered real task IDs/counts, same-origin session-cookie fetch
behavior, and tokenless mutation headers. They do not run a live browser or a
browser CSS rendering engine; private pages serve a `[hidden]` override so the
retained form layout cannot reveal the hidden login form.

The independently reviewed startup command was exercised against the retained
installation on 2026-10-10. Both API and Workbench reported ready; authenticated
REST listed 76 real tasks. The retained database container, data mount and all
13 migration digests were unchanged. This verifies repeatable startup with the
existing containers; it does not qualify database restore or a new installation.
The coordinating session owns actual TLS wire checks, browser verification,
independent review, production launch, and a durable repository integration.
