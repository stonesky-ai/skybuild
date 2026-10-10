# SkyBuild service control

Run `python3 scripts/skybuild_services.py` for status. The default inventory is
`ops/services/inventory.json`. Use `--inventory /absolute/private/inventory.json`
for a reviewed deployment inventory. Use `--host wonko` to select one host.

Use `start`, `stop`, or `ensure-running` as the first argument. Start and
ensure-running start only stopped, existing services. They do not build images,
install files, recreate containers, restart running services, or launch workers.
Stop uses reverse inventory order. It affects only exact service identities.

The recorded inventory describes the observed deployment on 2026-10-10. Update
container IDs and image IDs after a reviewed runtime promotion. A mismatch
blocks actions. Never replace an identity pin to bypass a failed check.

User units require the actual user bus and exact fragment path. Transient units
are observation-only. Their original bounded controller owns their lifetime.
The existing Wonko host watch is transient. This command cannot renew it.
Worker capabilities are separate from active jobs. A completed one-shot worker
is not a persistent service. This command never replays a worker attempt.

Wowbagger does not yet have the reviewed managed runtime. Status reports this
missing prerequisite. Its deployment, fresh host watch, scoped API identities,
controller profile, pinned image, permits and qualified task inputs must precede
worker launch. The current runtime targets Wonko; host qualification must precede
a Wowbagger deployment. No owner token goes to a worker container.

Output is JSON. A host failure does not hide the other host results. Driver
output stays private. Exit 1 means a service is missing, stopped, unready, or
unknown. After stop, an observed stopped service counts as success. A readiness
check is immediate; run status after the service startup interval. This command
retains the 8 GiB host reserve for container starts. It does not grant publication,
migration, model, task dispatch, or billing authority.
