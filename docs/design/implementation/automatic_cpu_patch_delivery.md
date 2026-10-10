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
      "token_file": "/absolute/private/worker-a-token",
      "git_token_file": "/absolute/private/worker-a-git-token"
    },
    {
      "worker": "worker_b",
      "brief_path": "docs/design/assignments/worker-b.json",
      "patch": "/absolute/private/worker-b.patch",
      "patch_sha256": "<64 hex>",
      "token_file": "/absolute/private/worker-b-token",
      "git_token_file": "/absolute/private/worker-b-git-token"
    }
  ]
}
```

Run from a checkout of the current published development base, after checking
the private API, two exact scoped worker principals, and the owner approval
deadline. Use a new private run directory outside every checkout:

```sh
scripts/project_python -m skybuild.auto_patch_controller \
  --checkout /absolute/skybuild-checkout \
  --manifest /absolute/private/candidates.json \
  --project skybuild --dispatcher pilot_dispatcher \
  --url https://controller.tailnet.ts.net:8443 \
  --dispatcher-token /absolute/private/dispatcher-token \
  --ca-file /absolute/private/ca.crt \
  --base-ref refs/heads/dev-006 \
  --approved-until '<current approved offset-aware cutoff>' \
  --state-dir /absolute/private/new-run-directory
```

The controller selects the two highest-priority eligible tasks from committed
brief candidates. It records selection, dispatches through the existing
durable Cord sender, then starts two separate one-shot worker processes. Each
worker checks its exact task approval, receives and fences a claim, renews its
lease, applies its patch in an independent clone, checks source bytes and Git
lineage, pushes its own branch, submits the fenced REST result, and sends its
Cord result. A failed or uncertain write leaves private evidence. Do not
restart a used run directory or silently replay a write.

Workers receive their scoped REST worker token and a separate Git token file
for the task-branch push. The controller does not pass a publisher credential
to them. This deterministic route never executes
candidate Python, project tests, Git hooks, or candidate configuration. The
worker Git identity must be qualified for its task-branch push. Keep gate and
publisher credentials in the separate trusted delivery context. A same-user
test process is not a security boundary for arbitrary candidate code.

After both exact heads reach `Validating`, collect results through the existing
current-task result verifier. Record all required validation stages and obtain
separate exact-head reviews. Freeze the reviewed heads together with
`prepare_bundle.py` or `marshall_bundle.py`; run the default full combined
disposable PostgreSQL gate on the frozen candidate. Publish only with the
qualified trusted publisher and confirm target tree and each member's inclusion
before REST acceptance. These later steps cannot be attested by worker output.
