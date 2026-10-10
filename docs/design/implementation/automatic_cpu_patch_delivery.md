# Bounded automatic CPU patch delivery

This route runs two useful, administrator-approved deterministic patch tasks. It
uses the existing REST task authority, committed assignment briefs, Cord,
fenced claims, and worker result contract. It does not run candidate code or
qualify a model author, reviewer, publisher, or generic worker sandbox.

Each task must be Ready in the Petri workflow. Its acceptance criteria must
explicitly contain `deterministic approved patch`. An administrator records
`metadata._skybuild_cpu_patch` as:

```json
{"schema":"skybuild.cpu-patch.v1","sha256":"<approved-patch-sha256>","assignment_id":"<committed-brief-assignment-id>"}
```

The committed `manual-work-brief-v1` names one task, one worker principal, a
dedicated task branch, and exact owned files. Only existing tracked regular
files are eligible. The patch file is at most 64 KiB, has an exact SHA-256, and
must be unreadable for modification by group or other users. Two selected tasks
need different workers, branches, assignment IDs, and non-overlapping files.

Supply a private JSON manifest outside the repository:

```json
{
  "schema": "skybuild.auto-patch-candidates.v1",
  "candidates": [
    {
      "worker": "worker_a",
      "brief_path": "docs/design/assignments/worker-a.json",
      "patch": "/absolute/private/worker-a.patch",
      "patch_sha256": "<64 hex>",
      "token_file": "/absolute/private/worker-a-token"
    },
    {
      "worker": "worker_b",
      "brief_path": "docs/design/assignments/worker-b.json",
      "patch": "/absolute/private/worker-b.patch",
      "patch_sha256": "<64 hex>",
      "token_file": "/absolute/private/worker-b-token"
    }
  ]
}
```

Run from a checkout containing the reviewed controller source and committed
briefs, with `origin/dev-006` at the current published development base. The
owner also supplies a private controller profile pinned to the exact reviewed
controller commit, loaded authority module digests, project, API endpoint, and
Python interpreter digest; the bridge rejects any source or profile mismatch. An
owner approves an exact private permit file by SHA-256. Its fields are
`schema=skybuild.auto-cpu-patch-permit.v1`,
`profile=bounded-trusted-cpu-patch-v1`, `project_id`, `host_id` (the execution
host's hostname), `source_head`, `base_ref`, `slots=2`, `approved_until`,
`usage`, `hostwatch_reserve_bytes`, `memory_high_bytes`,
`memory_max_bytes`, `runtime_seconds`, `worker_image_id`, and `workers`. The
image pin is the local Docker image ID in `sha256:<64 lowercase hex>` form. The ordered `workers`
array holds each selected `task_id`, `worker`, `assignment_id`, `brief_path`,
`brief_sha256`, `branch`, `base_sha`, `revision`, `patch_sha256`, and canonical
`envelope_sha256`. The executing checkout must be clean at the exact approved
head and all loaded SkyBuild and job-unit modules must resolve inside it.
Dispatch and claim compare the original selected envelope, including its
published base and committed brief digest. The `usage` pin contains `path`,
`sha256`, and `valid_until`, plus an optional `owner_policy` pin containing
`path` and `sha256`. The usage path must equal the controller's `--weekly-usage`
argument. The exact observation must remain truthful and valid throughout the
approved window. An owner revision must use schema
`skybuild.usage-policy-owner-revision.v1`, explicitly set `production_allowed`
to true, and explicitly set `weekly_production_stop_percent` to null. Only this
hash-pinned revision removes the historical 50% cutoff; it does not change
usage timestamps or other admission controls. Both evidence files are reread
before each controller effect. Without an owner revision, the historical
under-50% and no-drain requirements remain. Legacy permits containing
`weekly_usage_sha256` instead of `usage` retain those historical requirements;
a permit cannot contain both forms. The
hostwatch sample must be fresh, status `ok`, retain at least 8 GiB after both
unit ceilings, and report capacity for both jobs when present. Refresh this
sample before dispatch and each claim and launch. Use a new private run
directory outside every checkout:

```sh
scripts/project_python -m skybuild.auto_patch_controller \
  --checkout /absolute/skybuild-checkout \
  --manifest /absolute/private/candidates.json \
  --project skybuild --dispatcher pilot_dispatcher \
  --url https://controller.tailnet.ts.net:8443 \
  --dispatcher-token /absolute/private/dispatcher-token \
  --ca-file /absolute/private/ca.crt \
  --base-ref refs/heads/dev-006 \
  --owner-token /absolute/private/owner-token \
  --permit /absolute/private/approved-permit.json \
  --permit-sha256 '<owner-approved 64 hex digest>' \
  --weekly-usage /absolute/private/weekly-observation.json \
  --hostwatch /absolute/private/fresh-hostwatch.json \
  --controller-profile /absolute/private/controller-profile.json \
  --state-dir /absolute/private/new-run-directory
```

The controller selects two highest-priority eligible tasks from committed
brief candidates. It records selection, dispatches through existing durable
Cord sender, then claims each task with its own scoped worker principal. It
reserves one CPU unit against that exact live claim and prepares durable
dispatch effect before starting separate bounded systemd user units through
`JobUnitManager`. Each unit runs Docker with network disabled, read-only root,
read-only source and input mounts, one private output mount, dropped
capabilities, no-new-privileges, and permit-pinned memory, CPU, PID, and runtime
limits. The exact image must already exist locally.

Worker container receives no REST or Git credentials. It applies only the
hash-pinned patch to an independent local clone, checks file scope, bytes,
syntax, and parent commit, then writes terminal evidence and exits. The trusted
host controller validates exact terminal output and independently rebuilds the
expected tree from the approved patch. It writes a durable create-only push
intent, uses host Git authentication to push the exact task branch, and verifies
the remote head. On uncertain push outcome, it performs read-only reconciliation
and never retries the write. Host credentials never enter the container.

After confirmed push, the CPU bridge validates exact natural systemd
invocation and container exit, records terminal CPU observation, renews claim,
and settles existing effect and reservation before submitting the same fenced
result through existing workflow and Cord idempotency keys. A failed or
uncertain write leaves private evidence and held exposure. This one-shot run
requires explicit owner reconciliation after process restart; do not start a
replacement unit or reuse a consumed run directory.

The begin endpoint also rechecks unresolved task-lineage usage while holding the
project graph lock. Until the shared usage-history source from schema migration
016 is installed, begin fails closed with `usage_guard_unavailable`. Production
qualification requires the combined 014/015/016 schema and source bundle.

The controller uses scoped worker REST credentials only for task claim and
fenced workflow operations. Host Git authentication stays in trusted controller
process; container receives no host mounts, Docker socket, publisher credentials,
or network access. This deterministic route never runs candidate Python,
project tests, Git hooks, or candidate configuration. Keep gate and publisher
credentials in separate trusted delivery context.

After both exact heads reach `Validating`, collect results through the existing
current-task result verifier. Record all required validation stages and obtain
separate exact-head reviews. Freeze the reviewed heads together with
`prepare_bundle.py` or `marshall_bundle.py`; run the default full combined
disposable PostgreSQL gate on the frozen candidate. Publish only with the
qualified trusted publisher and confirm target tree and each member's inclusion
before REST acceptance. These later steps cannot be attested by worker output.
