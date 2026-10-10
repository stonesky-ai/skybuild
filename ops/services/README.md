# SkyBuild service control

Run `python3 scripts/skybuild_services.py` for status. The default inventory is
`ops/services/inventory.json`. Use `--inventory /absolute/private/inventory.json`
for a reviewed deployment inventory. Use `--host wonko` to select one host.

Use `start`, `stop`, or `ensure-running` as the first argument. Start and
ensure-running start only stopped, existing services. They do not build images,
install files, recreate containers, restart running services, or launch workers.
Stop uses reverse inventory order. It affects only exact service identities.
Start waits up to 20 seconds for each newly started container. It never repeats
a start. Status reports persistent services, observations, and capabilities
separately through the `managed`, `observation_only`, and `active_job` fields.
Stop skips observations and capabilities. It reports these skips explicitly.

The recorded inventory describes the observed deployment on 2026-10-10. Update
container IDs and image IDs after a reviewed runtime promotion. A mismatch
blocks actions. Never replace an identity pin to bypass a failed check.

User units require the actual user bus and exact fragment path. Transient units
are observation-only. Their original bounded controller owns their lifetime.
The existing Wonko host watch is transient. This command cannot renew it.
Worker capabilities are separate from active jobs. A completed one-shot worker
is not a persistent service. This command never replays a worker attempt.

Wowbagger now has the reviewed runtime and exact worker image. Its networkless
image smoke passed on that host. Its bounded managed watch runs for 60 minutes;
the service lifetime is 3700 seconds. The inventory observes this exact service.
It does not restart or renew the watch. Wonko's current watch is also bounded.

Runtime and image capability checks do not establish product-worker readiness.
Scoped API identities, controller profiles, permits and qualified task inputs
must precede worker launch. The first two-worker controller remains on Wonko.
No owner token goes to a worker container. No product job is active on Wowbagger.

Output is JSON. A host failure does not hide the other host results. Driver
output stays private. Exit 1 means a service is missing, stopped, unready, or
unknown. After stop, an observed stopped service counts as success. Status uses an immediate readiness check. Start uses a bounded readiness wait. This command
retains the 8 GiB host reserve for container starts. Container starts also require a same-host watch with status `ok` sampled within
90 seconds. The recorded Jeltz watch is historical; refresh or replace it with
a verified same-host watch before starting a stopped container.
It does not grant publication,
migration, model, task dispatch, or billing authority.
