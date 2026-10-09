# Retired SkyBuild task ledger

**Status:** retired on 2026-10-09. This file is a notice, not a task list, export, or writable authority. Do not add or import tasks here.

The authenticated SkyBuild REST API is the sole task authority for project `skybuild`. Read current tasks from `/api/v1/projects/skybuild/tasks` using the private endpoint and credentials configured for your enrolled client. See [architecture section 5](architecture.md#5-bootstrap-rest-and-task-contract) for authentication and scope rules.

The frozen three-ledger source was imported from commit `d79d2e1947d2c8e9edb577ab5f5093edfa3c94e3` with content digest `307019c967c0531912be0441809da8d89ca506905ff63594976bf5463ba142c7` and import digest `b25770457bc3c55d5697e67cb582a302ed39712b2693c3218e0079517376663a`. The API contains 38 tasks: 8 in progress, 12 proposed, 16 deferred, and 2 imported as done. Their original briefs and evidence remain in Git history. Imported `done` status records historical ledger state; it is not a new owner completion attestation.

Do not restore this snapshot as a writer if the API is unavailable. Recover API authority from the dedicated database and its verified backup procedure. These notices do not synchronize with the API.
