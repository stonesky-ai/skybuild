# Frozen bundle preparation

Task: `SKYBUILD-BUNDLED-INTEGRATION`. Implementation source base:
`42c3256d1dd05af2c1c457d214231cce9f69c066` (architecture A38, working
contract and automatic bundled integration in section 2). This is the bounded
preparation slice of the existing integration workflow, not a new readiness
authority. The Markdown ledgers retain task authority.

`scripts/prepare_bundle.py` composes only caller-supplied, already-reviewed full
commit SHAs, in supplied dependency order, into a detached candidate. It checks
the exact advertised target/member refs before fetching, after fetching and
after composition. Fetch writes objects without updating shared refs or
`FETCH_HEAD`. Successful output records the exact base, ordered task membership,
reviewer identities, review/policy artifact hashes, member changed paths,
candidate commit and tree. Member inclusion is proved by Git ancestry. Paths
are observations, not inferred scope approval.

The caller provides a JSON manifest with this shape (replace every example SHA
and reference with its exact actual value):

```json
{
  "schema": "skybuild.bundle-input.v1",
  "target_ref": "refs/heads/dev-002",
  "base_sha": "0000000000000000000000000000000000000000",
  "policy_evidence": "accepted-bundle-requirements.md",
  "members": [
    {
      "task_id": "SKYBUILD-EXAMPLE",
      "ref": "refs/heads/task/example",
      "head_sha": "1111111111111111111111111111111111111111",
      "review": {
        "verdict": "pass",
        "head_sha": "1111111111111111111111111111111111111111",
        "reviewer": "qualified-independent-session-reference",
        "evidence": "example-exact-head-review.md"
      }
    }
  ]
}
```

Evidence paths resolve relative to the manifest. Each review artifact must be
nonempty and contain the exact full member SHA. The explicit passing verdict
and reviewer reference are caller attestations to existing independent review;
the helper cannot qualify a reviewer or interpret prose findings. Supply the
accepted project/member gate requirements as `policy_evidence`; its bytes are
hashed with the inputs, not interpreted or weakened. The existing review and
one full combined disposable gate remain required. The 64-member and 256 KiB
input limits bound one invocation, not project batching policy.

Run from a clean, explicitly owned SkyBuild checkout whose origin fetch/push
URLs identify `stonesky-ai/skybuild`. Use a new output directory outside every
existing worktree; its parent must already exist:

```sh
python scripts/prepare_bundle.py \
  --checkout /absolute/owned/skybuild-checkout \
  --manifest /absolute/evidence/bundle-input.json \
  --output /absolute/scratch/frozen-bundle-001
```

Git commands run at nice 10. Before fetch/worktree creation and each merge,
available memory must be at least 8 GiB. The helper uses no model, REST endpoint,
daemon, PostgreSQL process, test gate, publisher or runtime deployment.

The output contains `inputs.json`, `report.json`, an invocation lock and the
retained `candidate` worktree. A successful rerun with identical frozen inputs
returns the original report after rechecking remote refs, candidate cleanliness,
detached ownership, commit and tree. Changed review/policy bytes or membership
require a new output. Changed remote refs never silently substitute a new head.
The final ref observation is not a publication-time expected-base guarantee;
the existing integration workflow must recheck its own publication conditions.

Conflicts and other preparation failures retain their report and candidate,
including an unresolved index and conflict paths where available. The helper
never resets, aborts, deletes or adopts another worktree, and never modifies a
completed report on failed revalidation. An interrupted or failed output is not
automatically retried; diagnose it, preserve evidence and explicitly prepare a
new candidate. Keep output artifacts with the bundle handoff. Candidate review,
the full combined gate, publication and confirmed per-task inclusion follow as
separate operations under the existing policy.

Reuse is deliberately narrow: SkyBuild `integrate_reviewed_pr.py` supplies the
guarded exact-ref/detached-candidate mechanics and `_repo_guard` is reused
directly. SkyKeep `383d3d375979c66b39df0f61c19a165957fecdcf`
`mergeprep.py` contributes the `outside_scope`, `seam_facts`, `run_merged_tree`
and `readiness_doc` concepts: explicit membership, path facts, isolated
composition and concise durable evidence. Its daemon, legacy task authority,
launch paths and publication behavior are not copied.
