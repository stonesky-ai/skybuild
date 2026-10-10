# Pinned task workflow preflight probe

`skybuild.fleet_preflight` can optionally read one selected task's Petri workflow before a worker receives an assignment. The probe uses the existing authenticated `Client`, trusted HTTPS settings, and the worker's existing project-scoped read grants. It adds one bounded `GET /api/v1/projects/{project_id}/tasks/{task_id}/workflow` request.

Pass `--task-id` to select a task. Pin any known values with `--expected-task-revision`, `--expected-input-generation`, `--expected-definition-revision`, and `--expected-policy-version`. The probe validates the returned workflow token with `TaskToken.from_dict`, checks the token and task projection identify the requested project and task, checks their revisions agree, and compares every supplied expected value. It rejects absent, malformed, unsupported, or mismatched workflow data.

Example:

```sh
rtk proxy scripts/project_python -m skybuild.fleet_preflight \
  --url https://controller.tailnet-name.ts.net:8443 \
  --project skybuild \
  --token-file /path/to/private-worker-token \
  --principal worker-principal \
  --ca-file /path/to/approved-public-ca.crt \
  --task-id SKYBUILD-WORKFLOW-PREFLIGHT-PROBE \
  --expected-task-revision 2 \
  --expected-input-generation 1 \
  --expected-definition-revision 1 \
  --expected-policy-version petri-checks-v1
```

Expected values are optional when selecting a task. If an expected value is supplied without `--task-id`, the command fails before connecting. The compact JSON result reports task ID, revision, input generation, definition revision, policy version, and workflow place. It omits the task title, description, workflow record, and token. Failures print one generic message.

The probe does not require `Ready`, claim the task, or grant execution permission. Its `execution_authorized` result is always `false`. A successful response verifies only workflow identity and supplied input pins. The existing `--workflow` flag remains separate: it checks the explicit Petri worker credential profile, including claim and write grants. Omit that flag when using this read-only probe with the existing worker read scopes.

Without `--task-id` or expected workflow values, existing preflight requests and output remain unchanged.
