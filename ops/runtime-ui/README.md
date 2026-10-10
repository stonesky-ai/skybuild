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
another database. The runtime Compose file pins the Workbench source mount to
this checkout. Moving the installation requires a separate reviewed configuration.

Open `https://jeltz.tail991ac1.ts.net:8443/workbench` with the installation CA
trusted. The certificate names this private hostname. Workbench also publishes
port 8443 on loopback; verified loopback probes retain the certificate hostname
with `curl --resolve`. The API publishes port 8000 only on loopback. Workbench
publishes on loopback and the explicit installation Tailscale interface.

Default gateway mode uses an existing authorized project token entered in the
browser. Starting the stack does not start a worker, dispatcher, inference
process, or usage collector. Those processes retain their own task, credential,
budget and lifecycle controls.

## Optional private MVP mode

The owner-directed private MVP mode removes browser token entry for one configured
project. Enable it only by passing both explicit options to `runtime_ui.py`:

```sh
--workbench-token-file /run/secrets/skybuild-workbench-token \
--workbench-project skybuild
```

Mount the selected token as a read-only regular file owned by the Workbench process
user with mode `0600`. The gateway rejects symlinks, group/world-readable files,
other owners, malformed values, or a missing option. Do not mount or auto-select
the pilot owner-token path. Keep the token outside Git, browser assets, HTML,
responses and logs. The browser receives only the configured project ID.

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
remains empty. Tokens stay in browser memory and are supplied by the user.

Routes:

- `/workbench` and `/workbench/tasks`: current task layout backed by REST.
- `/workbench/workflow`: authoritative Petri workflow board and transitions.
- `/api/v1/*` and `/health/*`: fixed-upstream REST proxy.
- `/workbench/runtime`: non-secret composition identity.
- Unwired status, milestone, process-control, and sample-data routes return 503
  with an explicit unavailable message. They never present sample status as live.

Run inside the existing pilot Docker network using the already installed
FastAPI/httpx/Uvicorn dependencies. Mount this artifact at `/runtime-ui:ro` and
the installation TLS directory at `/tls:ro`.

```sh
python /runtime-ui/runtime_ui.py \
  --ui-checkout /runtime-ui/ui \
  --api-checkout /runtime-ui/api-source \
  --ca-file /tls/ca.crt \
  --backend-hostname jeltz.tail991ac1.ts.net \
  --backend-connect-host api --backend-port 8000 \
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

Result: 44 passed. These checks cover routes, empty lists, installation trust,
SNI, fixed destination, caller and server credential boundaries, task CRUD/history/
workflow loading, mutation intent, request limits, unavailable panels, origin/Host
checks, redirects, sanitized failures, qualified database checks, startup ordering,
changed-asset refusal, oversized upstream responses, refusal after readiness failure,
private listener boundaries, and executed Node VM DOM tests for both generated
private page scripts. Those tests verify automatic task/workflow-board reads,
rendered real task IDs/counts, and tokenless mutation headers. They do not run a
live browser or a browser CSS rendering engine; private pages serve a `[hidden]`
override so the retained form layout cannot reveal the hidden login form.

The independently reviewed startup command was exercised against the retained
installation on 2026-10-10. Both API and Workbench reported ready; authenticated
REST listed 76 real tasks. The retained database container, data mount and all
13 migration digests were unchanged. This verifies repeatable startup with the
existing containers; it does not qualify database restore or a new installation.
The coordinating session owns actual TLS wire checks, browser verification,
independent review, production launch, and a durable repository integration.
