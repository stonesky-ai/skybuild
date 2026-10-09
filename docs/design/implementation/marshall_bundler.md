# Marshall bundler

Implementation brief for `SKYBUILD-MARSHALL-BUNDLER`. This is a one-shot operator tool for reviewed task heads, using REST task authority and existing frozen bundle preparation. It does not launch workers, claim tasks, update task state, or publish Git refs.

## Adapted mechanics and provenance

The related-work scoring, bounded groups, explicit in-flight exclusion, isolated premerge, and gate evidence are adapted from SkyKeep's bundler design. SkyKeep-specific queues, overseer loops, provider configuration, vault bindings, and seam authority are not imported.

Sources inspected:

- `scripts/seam_bundle.py` at SkyKeep checkout commit `2aed3a3416cf1cf49d92fc6f945bde5514963a60`; file SHA-256 `5767469edb2fee56c1d4f829f2e0ac47ab36cc972ce4ac83c5069bcee814227d`.
- Overseer `bundler/bundler.py`; file SHA-256 `e8a6760d7636061e881b0545b3a3951e941f7d3681f6bb8ff415eed42d95699b`.

## Inputs and authority

Supply a bounded JSON catalog (256 KiB, 1–100 explicit reviewed heads). Paths to policy and review evidence are relative to the catalog. Every review must name the exact head, a passing verdict, reviewer, and evidence file containing that head. The operator must establish reviewer qualification under the review policy; a JSON assertion does not establish qualification.

```json
{
  "schema": "skybuild.marshall-input.v1",
  "target_ref": "refs/heads/dev-003",
  "base_sha": "FULL_40_CHARACTER_LOWERCASE_COMMIT",
  "policy_evidence": "policy.md",
  "members": [{
    "task_id": "SKYBUILD-EXAMPLE",
    "ref": "refs/heads/task/example",
    "head_sha": "FULL_40_CHARACTER_LOWERCASE_COMMIT",
    "review": {
      "verdict": "pass",
      "head_sha": "FULL_40_CHARACTER_LOWERCASE_COMMIT",
      "reviewer": "qualified-independent-reviewer",
      "evidence": "review.md"
    }
  }],
  "in_flight": [],
  "already_bundled": [],
  "ignored_paths": ["docs/**"]
}
```

The authenticated principal must match `--principal`. Each selected task is read from REST, including its revision, status, workflow phase, and dependencies. Only `ready` or `in-progress` tasks with explicit passing exact-head reviews enter planning. REST readiness enforces completed prerequisites and recursively invalidates dependents after prerequisite changes; the planner orders selected dependency edges but does not independently replace those authority checks. The tool does not invent a ready-for-integration API phase. Task status and revision are rechecked before preparation and after a passing gate.

The catalog must include every task currently ready for integration, up to the policy limit. Exclusion lists are explicit operator snapshots, not a distributed reservation or automatic discovery mechanism. Refresh them before each run. Missing or stale remote refs fail closed. Task ID prefixes do not imply related code.

## Planning and preparation

The planner scores shared changed paths and dependency edges, orders prerequisites first, and coalesces eligible work into groups of at most 20. Ignored path globs affect relatedness scoring only. Duplicate identities/refs, inconsistent bases, cycles, and stale reviews are rejected. A final singleton is allowed after preceding groups fill the cap.

Run from a clean SkyBuild checkout with a current clear host-watch sample and at least two worktree slots available. Use a new output directory outside existing worktrees:

```sh
python /absolute/skybuild/scripts/marshall_bundle.py \
  --checkout /absolute/skybuild \
  --catalog /absolute/evidence/catalog.json \
  --output /absolute/evidence/run-001 \
  --url https://private-host:8443 \
  --ca-file /absolute/tls/ca.crt \
  --token-file /absolute/secrets/owner-token \
  --project skybuild --principal pilot_owner \
  --prepare-next
```

The script imports its own checkout even when the interpreter belongs to another worktree. The endpoint must satisfy existing private-endpoint policy; credentials are read from a protected file and are not written into evidence.

Planning records REST snapshots, skip reasons, related paths, dependency edges, full frozen inputs, and candidate manifests. Validated policy/review bytes are copied into the output with their SHA-256 digests. Preparation freezes only the next group through `prepare_bundle.py`; later manifests are provisional and need a fresh base after preceding publication. Conflicts retain the candidate and child report. Existing outputs are never overwritten by a new run.

## Gate and handoff

`--gate-next` implies preparation and runs the existing disposable PostgreSQL combined gate with the owner's 6 GiB reserve. Gate success requires process success, `ok: true`, confirmed cleanup, unchanged remote refs, unchanged frozen evidence, a durable terminal gate artifact pinned to the exact run, head and tree, a clean candidate with the same head and tree, and unchanged REST task status/revision. Failure reports never claim publication.

A green report is a handoff for independent exact-candidate review and guarded publication through one frozen bundle PR. Publication and confirmed task inclusion remain required; this tool does not replace those steps. No gate evidence is reused automatically by the integration helper.

## Initial five-case evaluation

Five closely related synthetic reviewed branches are exercised with real isolated Git premerging in tests. These are fixtures, not production tasks. Start a real five-case group only when REST authority and independent exact-head reviews expose five eligible cases. Do not unblock tasks, fabricate reviews, or split unrelated work merely to reach five.
