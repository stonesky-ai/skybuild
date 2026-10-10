# Session snapshot helper

`scripts/session_snapshot.py` prints one bounded JSON snapshot for a selected SkyBuild checkout and one task. It reports the exact Git root, verified SkyBuild origin, head, branch and a short dirty-path summary. It reads one task workflow through the authenticated REST API and keeps only task ID, revision, status, place, next action, blocker, responsible party, evidence freshness, validation stage/state pairs and available action names.

The helper uses `scripts/_repo_guard.py`, `Client` and the protected token reader in `skybuild.fleet_preflight`. It makes one workflow GET request. The token stays in a private file and never appears in output. The API client disables ambient proxy settings, uses bounded request time and retries zero times, and accepts a caller-provided CA file for the private service.

By default, the helper does not inspect source files or invoke CodeGraph. `--graph-query` enables an optional CodeGraph status check followed by one caller-supplied `codegraph explore` query. The helper requires an existing checkout-local `.codegraph/` index and an exact `projectPath` match. Missing, mismatched or unavailable indexes produce an explicit unavailable result. The helper never initializes, syncs or fetches an index.

Example:

```sh
scripts/project_python scripts/session_snapshot.py \
  --checkout /home/kevin/my_code/skybuild \
  --project skybuild \
  --task-id SKYBUILD-SESSION-SNAPSHOT-HELPER \
  --url https://skybuild.example.ts.net:8443 \
  --token-file /private/path/to/owner-token \
  --ca-file /private/path/to/ca.crt
```

Add `--graph-query "Client task_workflow"` only when source navigation is needed and the exact checkout already has a local index. Each subprocess has a deadline and output cap. Failures appear as per-section `unavailable` results without raw exception text. The JSON contains observations only; it does not authorize a task action, deployment, worker launch or integration decision.
