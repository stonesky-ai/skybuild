# Isolated controller update rehearsal

This is a source and runtime qualification tranche of `SKYBUILD-MVP-CONTROLLER-UPDATE`. It does not deploy, migrate, or change the accepted pilot. The accepted controller source/image are pinned to `7da0ef6542f2a9aeae6feac529eca63b0b2d5bca` and `sha256:77bc01222c9c018d44968198e26d0be4618825b35ba07bb8c993002bce4f51c5`. `ec3fa9c12f560d205ddf46c08ccaef47012341c2` / tree `ec84d8ed278d7d8fbf8e8bb219cb64ddb48f934f` is the current candidate proposal only. The root-supplied exact published candidate commit, tree and ref must be passed explicitly after the reviewed bundle is frozen; the harness does not constrain future runs to ec3fa.

## Preparation

The preparation command verifies the owned clean checkout, exact published ref and commit/tree, package version, accepted API and PostgreSQL image identities, and every 001–013 migration digest against the accepted runtime evidence. It emits a deterministic runtime plan and makes no Docker, API, database, or network changes:

```sh
rtk proxy scripts/project_python scripts/controller_update_rehearsal.py \
  --checkout "$PWD" \
  --candidate-source ec3fa9c12f560d205ddf46c08ccaef47012341c2 \
  --candidate-tree ec84d8ed278d7d8fbf8e8bb219cb64ddb48f934f \
  --candidate-ref refs/heads/dev-006 \
  --accepted-runtime-evidence /tmp/skybuild-live-workbench-task-20261010/postmerge-runtime-audit.json
```

The runtime audit's `source_head` identifies the mounted workbench source; it is not treated as the API image's source. The accepted API source/image binding above is the reviewed controller baseline. The rehearsal checks the image and schema digests independently.

## Runtime procedure

The runtime command requires a fresh exclusive evidence file in an owner-private mode-0700 directory and a reviewed GO record bound to the exact serialized preparation plan. The GO record is prepared only after independent source review and root's resource admission/review of the exact execution plan. Example record shape:

```json
{
  "decision": "GO",
  "task_id": "SKYBUILD-MVP-ISOLATED-CONTROLLER-REHEARSAL",
  "plan_sha256": "<sha256 of the compact, sorted preparation JSON plus newline>",
  "accepted_image": "sha256:77bc01222c9c018d44968198e26d0be4618825b35ba07bb8c993002bce4f51c5",
  "postgres_image": "<current full accepted PostgreSQL image ID>",
  "accepted_api_container_id": "<current full accepted API container ID>",
  "accepted_pg_container_id": "<current full accepted PostgreSQL container ID>",
  "candidate_source": "ec3fa9c12f560d205ddf46c08ccaef47012341c2",
  "candidate_tree": "ec84d8ed278d7d8fbf8e8bb219cb64ddb48f934f",
  "reviewer": "<independent reviewer identity>",
  "root_authorizer": "<root authorizer identity>"
}
```

After that exact GO, invoke:

```sh
rtk proxy scripts/project_python scripts/controller_update_rehearsal.py \
  --checkout "$PWD" \
  --candidate-source ec3fa9c12f560d205ddf46c08ccaef47012341c2 \
  --candidate-tree ec84d8ed278d7d8fbf8e8bb219cb64ddb48f934f \
  --candidate-ref refs/heads/dev-006 \
  --accepted-runtime-evidence /tmp/skybuild-live-workbench-task-20261010/postmerge-runtime-audit.json \
  --execute --reviewed-go-record /path/to/private-reviewed-go.json \
  --evidence-output /path/to/private-evidence/controller-update.jsonl
```

The harness first performs read-only inspection of the accepted API and PostgreSQL container IDs/image IDs and requires exact matches to the GO record and runtime audit. It creates a unique internal-only Docker network and disposable PostgreSQL volume. It applies the exact migration set through `psql` inside that isolated database. A one-shot initializer then creates a restricted runtime role and separate synthetic owner/worker identities. The accepted API creates one synthetic task and one Cord message. Only the initializer and disposable PostgreSQL receive the temporary database administrator credential; API containers receive only the synthetic restricted runtime role. It starts a rehearsal instance of the immutable accepted image, builds a candidate overlay from the exact Git archive using the accepted image as the pinned base (`--pull=false`, `--no-cache`, `--network=none`), and records readiness probes while the build process is active. A bounded CPU-only build step keeps the build active long enough to measure responsiveness. The overlay only places published `src/skybuild` files ahead of the accepted image's installed package; this reuses the accepted dependency set without an unpinned base or package download. No live mounts, live credentials, host ports, or external network are supplied.

It then acknowledges stopping the accepted API, injects a candidate startup failure, and records the failed container identity. The harness prints one exact `docker start <full accepted-container-ID>` command and waits for an operator to run it in a separate shell and acknowledge the same ID. It verifies the accepted API is running and ready, then compares the synthetic task and Cord journal fingerprints. It stops the accepted API again before starting the candidate normally, confirms authenticated identity/readiness/history, and records the immutable image/container IDs and every writer transition. Any uncertain stop, start, journal write, or cleanup fails the run; evidence is retained and the operator must inspect residue before retrying. The image remains identified by its unique rehearsal tag for evidence review.

## Limits

This proves a controlled update and manual recovery only in a fresh synthetic database. It does not test the live controller, captured production task history, live credentials, TLS/Serve configuration, persistent volume restoration, automatic deployment, worker admission, or production disaster recovery. It cannot qualify the full MVP by itself. The runtime UI feature added in PR79 is separate from `src/skybuild`; its live source/runtime verification is in the workbench post-merge audit and must not be attributed to this API-image rehearsal.
