# Manual Petri integration attestations

Architecture A44 governs this bounded owner/admin bridge. It records actual frozen-bundle publication evidence through REST. It does not qualify the automatic publisher, perform a merge, run a gate, launch a worker or independently contact GitHub inside a database transaction.

The authenticated owner attests external observations. The producer checks Git/GitHub and retained artifacts before preparing that statement; the server checks its exact shape and consistency with current task state and the original freeze. Same-UID host files are not an isolation boundary. This scope requires no new receipt mount or signing key and makes no unforgeability claim.

## Prerequisites and evidence

Use a qualified Petri runtime and canonical SkyBuild checkout. Preserve its deployment receipt separately. The owner credential must be a private ordinary token file; keep tokens out of arguments, packets and logs. Use certificate-verified private Tailscale HTTPS and the approved public CA certificate. Store packets in a user-owned mode-0700 absolute directory outside worktrees; packets are created once as mode-0600 files.

Before freeze, the task is Validating with current satisfactory evidence for every required stage and no unresolved control. Preserve the original `project_id`, `task_id`, `attempt_id`, `claim_fence`, `input_generation`, `definition_revision`, `policy_version`, `source_head`, `source_branch` and `target_base`. Changed inputs require reassessment and fresh evidence.

Record actual `unit_tests`, `scans`, `long_tests`, `code_review` and `needs_rebase` results before freeze. Run any required author validation gate before recording its stage result. The preparation ancestry/tree checks below do not replace those stages. The separate combined publication gate still runs after freeze through the reviewed integration helper; this bridge neither skips that gate nor treats an earlier author check as equivalent combined evidence.

Prepare the actual bundle with `scripts/prepare_bundle.py` under the existing worktree/headroom, review and host-watch controls. Retain `inputs.json`, `report.json`, policy and exact-head review files. The producer checks the frozen manifest fingerprint and artifact hashes, unique membership, pushed member heads, candidate tree and member/base ancestry. The currently published target ref must still equal the frozen base before freeze. Keep every ready reviewed task that fits under the normal bundle policy.

The route is owner/admin-only:

`POST /api/v1/projects/{project_id}/tasks/{task_id}/manual-integration`

It accepts only publication-required `freeze`, `accept` and `integration_progress` for an unknown publication outcome. The exact evidence schema is `skybuild.manual-integration.v1`; the packet includes event, expected revision, operation ID, attestor, `manual_owner_attestation` authority, task binding, validation digest, bundle and event-specific evidence. The server derives guard facts itself. Ordinary worker workflow calls cannot use this bridge to accept a task.

## Prepare and submit freeze

Set the following paths to approved private state and actual retained preparation. The examples use variables for paths and IDs; they contain no credentials.

```sh
rtk proxy "$SKYBUILD_CHECKOUT/scripts/project_python" \
  "$SKYBUILD_CHECKOUT/scripts/manual_integration_receipt.py" \
  --url "$SKYBUILD_URL" --project skybuild --task-id "$SKYBUILD_TASK" \
  --token-file "$SKYBUILD_OWNER_TOKEN_FILE" --ca-file "$SKYBUILD_CA" \
  freeze --checkout "$SKYBUILD_CHECKOUT" --prepared "$SKYBUILD_PREPARED" \
  --operation-id "$SKYBUILD_FREEZE_OPERATION" --output "$SKYBUILD_FREEZE_RECEIPT"

rtk proxy "$SKYBUILD_CHECKOUT/scripts/project_python" \
  "$SKYBUILD_CHECKOUT/scripts/manual_integration_receipt.py" \
  --url "$SKYBUILD_URL" --project skybuild --task-id "$SKYBUILD_TASK" \
  --token-file "$SKYBUILD_OWNER_TOKEN_FILE" --ca-file "$SKYBUILD_CA" \
  submit --receipt "$SKYBUILD_FREEZE_RECEIPT"
```

The first command prepares a private packet and reports `submitted: false`. The second explicitly submits those same bytes with the packet's operation ID and expected revision. Confirm the workflow moved Validating to Integrating and inspect history for the complete envelope, canonical SHA-256 and manual provenance. Preserve the packet; acceptance must match its exact bundle, candidate/tree/base, member set, task binding and validation snapshot.

## Publish, then prepare acceptance

Push the frozen candidate, open its bundle PR, obtain independent exact-candidate review and use `scripts/integrate_reviewed_pr.py` with its required default full disposable PostgreSQL gate. Retain the integration JSON and its terminal gate artifact. Confirm gate success, default command, exact tested tree and cleanup; a passed check without cleanup confirmation is insufficient.

```sh
rtk proxy "$SKYBUILD_CHECKOUT/scripts/project_python" \
  "$SKYBUILD_CHECKOUT/scripts/manual_integration_receipt.py" \
  --url "$SKYBUILD_URL" --project skybuild --task-id "$SKYBUILD_TASK" \
  --token-file "$SKYBUILD_OWNER_TOKEN_FILE" --ca-file "$SKYBUILD_CA" \
  accept --checkout "$SKYBUILD_CHECKOUT" \
  --freeze-receipt "$SKYBUILD_FREEZE_RECEIPT" \
  --integration-artifact "$SKYBUILD_INTEGRATION_ARTIFACT" \
  --operation-id "$SKYBUILD_ACCEPT_OPERATION" --output "$SKYBUILD_ACCEPT_RECEIPT"

rtk proxy "$SKYBUILD_CHECKOUT/scripts/project_python" \
  "$SKYBUILD_CHECKOUT/scripts/manual_integration_receipt.py" \
  --url "$SKYBUILD_URL" --project skybuild --task-id "$SKYBUILD_TASK" \
  --token-file "$SKYBUILD_OWNER_TOKEN_FILE" --ca-file "$SKYBUILD_CA" \
  submit --receipt "$SKYBUILD_ACCEPT_RECEIPT"
```

Acceptance preparation checks the merged PR repository/head/base/merge commit, actual published tree, current target reachability and inclusion of every frozen member. It binds the real gate and artifact hashes and produces the existing completion evidence contract. The server also requires current satisfactory validation and the original journaled freeze. Observe Integrating to Done through REST and retain the immutable history receipt. Git merge alone does not complete the task.

## Unknown publication and retries

If merge acknowledgment is unknown, preserve the operation reference and original artifacts. Prepare an unknown observation rather than accepted completion:

```sh
rtk proxy "$SKYBUILD_CHECKOUT/scripts/project_python" \
  "$SKYBUILD_CHECKOUT/scripts/manual_integration_receipt.py" \
  --url "$SKYBUILD_URL" --project skybuild --task-id "$SKYBUILD_TASK" \
  --token-file "$SKYBUILD_OWNER_TOKEN_FILE" --ca-file "$SKYBUILD_CA" \
  unknown --freeze-receipt "$SKYBUILD_FREEZE_RECEIPT" \
  --publication-operation "$SKYBUILD_PUBLICATION_OPERATION" \
  --operation-id "$SKYBUILD_UNKNOWN_OPERATION" --output "$SKYBUILD_UNKNOWN_RECEIPT"
```

Submit that packet explicitly with the same `submit --receipt` command. Unknown publication retains Integrating and ownership. Reconcile actual publication before acceptance or any competing publication.

After an uncertain REST reply, retry the identical saved packet. Never change its expected revision, operation ID or contents to force a replay through. Read workflow/history to distinguish a confirmed replay from a conflicting or stale operation. A failed preparation does not authorize manufacturing packet fields or claiming acceptance.

## Wonko CPU proof and acceptance boundary

The committed [Wonko brief](../assignments/wonko-rest-worker-smoke-20261010.json) is task content, not dispatch authority. After runtime and credential qualification, create its REST task, pin the current published dev base and current task revision in a Cord assignment, and run one bounded CPU invocation on Wonko. The invocation receives, claims, renews before expiry, prepares an owned worktree, appends exactly one timestamp line, checks/commits/pushes and submits its output. Retain its durable claim/submission binding for result freshness. No person must edit the README in advance.

Independent components then record actual required validation and separate exact-head review. The owner bridge attests the real bundle freeze and publication as above. Verify REST Done/history, then close the dev-to-main cycle with its required review and gate and confirm the same source is included in main. Keep the original dev receipt and retain the main observation in the final evidence manifest and a Cord message referencing the stable task ID. Do not PATCH completed-task metadata to add that observation: ordinary task updates invalidate inputs. Preserve the stopped worker and all task, Cord, claim, validation, gate, cleanup and publication evidence. One successful CPU proof does not qualify the fake-only dispatcher, a fleet daemon, model launching or autonomous owner attestation.
