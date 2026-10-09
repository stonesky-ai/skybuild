# Reusable code and capability research

Read-only planning inspection, 2026-10-08. This note supports architecture revision A3. It is not a deployment/capability test and does not authorize implementation or model calls. The SkyKeep checkout contains unrelated local edits; file observations are current on-disk source at inspection, not necessarily committed or installed.

## Source ideas to retain

| Inspected source | Useful component or constraint | Proposed extraction treatment |
| --- | --- | --- |
| scripts/todo_service/app.py: app_from_env, open_store, create_app | Existing FastAPI auth, hashed bearer lookup, URL-token refusal, size/config/error handling and store boundary. | Reuse bounded HTTP behavior/contracts and tests; separate neutral API identity/config from SkyKeep deployment literals. |
| scripts/todo_service/store.py: Store._write, _read, init, list_items | Explicit SQLite transactions/WAL/schema guard; list_items also expires claims in a write transaction. | Treat hidden writers explicitly; replace serialization with deliberate PostgreSQL semantics, not textual SQL substitutions. Main source is older than deployed v19. |
| scripts/todo_service/client.py: TodoClient | Existing shared client called by workers/admin paths. | Preserve client compatibility where useful; one versioned contract for new task/mailbox operations. Source location was found by CodeGraph; no complete client review occurred. |
| scripts/agents/session_keeper.py: Config.todo_words, choose_engine, SystemdSpawner | Existing model-size mapping, escalation behavior, systemd process control and bounded subprocess failures. | Adapt capabilities and process identity; remove implicit paid escalation/restart authority. Keep engine selection pure and auditable. |
| scripts/gate_distribute.py: endpoint_lane_cap, lane_order, widest_first | Explicit cross-box endpoint lane cap and CPU suite distribution; short suites can reuse an existing lane to avoid bring-up cost. | Reuse deterministic planning ideas; measure endpoint bottleneck independently from CPU slots and avoid inferring safe defaults from old timings. |
| scripts/gate_batch_split.py: split, probe_verdict, scrubbed_env | CPU-driven isolation of failing integration candidates, base/candidate checks, UNMEASURED distinction and environment/checkout safeguards. | Reuse diagnosis before paid reasoning; retain exact-revision/evidence and do not blame a seam when baseline/infrastructure is unresolved. |
| scripts/endpoint_shape.py: OpenAIEndpoint, chat_body, Reply, read_completion | Bounded HTTP result, auth/protocol configuration, max-output field mapping and injectable transport. | Extract a small neutral adapter; existing imports into skykeep.models prevent simply copying it as a standalone SkyBuild client. Distinguish missing usage from a real zero in the new contract. |
| scripts/aragog/start.sh | Stops a running VM before changing max-run-duration; startup/backstop configuration is external. | Preserve runtime-limit idea; refuse implicit restart and reconcile work before any restart. Do not copy secrets/startup payloads. |
| scripts/aragog/stop.sh | Claim guard is conditional on queue URL/token configuration. | Unknown queue/claim state must refuse ordinary stop or require an explicit emergency path, rather than look like an empty queue. |
| scripts/aragog/status.sh | Queries provider lifecycle as well as host facts. | Retain provider-observed status; SSH failure alone does not establish stop/billing state. |

Historical docs/dev/endpoint-queue.md records glm4:9b probes on 2026-10-02 and timeouts under ten lanes. It explicitly says the exact queue/reload cause and a safe lower lane count were not established. This supports shared endpoint accounting, not a claim about today's model or coding throughput. Earlier stream notes also describe model eviction/reload risk. No model name, GPU memory, live protocol, benchmark score or safe concurrency for the current LLM.brodson.net asset was verified.

Retained session miner: /tmp/skykeep-log-scanner, seam/LOG-REPEAT-SCANNER, scripts/agents/session_mine.py and tests/docs. Its completion/landing remains unresolved. No new scan ran.

## Tool capability checks

- RTK was used for this session's shell commands; ripgrep is available. Exact source/command output used RTK proxy where appropriate.
- CodeGraph status verified /home/kevin/my_code/skykeep before graph queries. Queries used that checkout and current on-disk source blocks. Its indexed main store is older than the deployed todo-live inventory. No index was created or refreshed. SkyBuild has no .codegraph index and none was created.
- Serena MCP is now exposed and its instructions/activation were callable in this session. Activation selected SkyKeep but reported no active language servers. A symbol-overview/config batch did not complete and was stopped; semantic navigation is not verified. Do not report it working merely because activation succeeded or reinstall automatically. Follow up with language-server/project configuration when relevant, without borrowing another session's state.
- pyright-langserver already existed under the owner's local bin; this session installed nothing. The separate Claude plugin's operation remains unverified. CodeGraph/raw-source fallback supported this inspection.
- SANY worked. Standard exhaustive TLC initialization hit the sandbox's local-listener restriction; final random simulation and both mutation traces ran. [Model notes](../models/README.md) state exact bounds/limitations.

These limits belong in future context packets: a stalled semantic tool should have a bounded fallback, not cause repeated paid attempts or an unsupported architectural inference.

## Current external facts used

Official primary documentation was checked on 2026-10-08. No price or hardware is selected; current offers must be checked at provision time.

- [RunPod management](https://docs.runpod.io/pods/manage-pods): start/stop/terminate are distinct; stop does not establish that retained storage is free or that restart capacity is guaranteed.
- [RunPod storage](https://docs.runpod.io/pods/storage/types) and [pricing](https://docs.runpod.io/pods/pricing): container vs attached/network volume persistence and residual charges differ. Keep important artifacts outside disposable compute.
- [RunPod endpoint settings](https://docs.runpod.io/serverless/endpoints/endpoint-configurations): active/min workers can incur idle compute charges, max workers bounds scale, and queue/timeout behavior needs adapter qualification.
- [GCE runtime limits](https://docs.cloud.google.com/compute/docs/instances/limit-vm-runtime), [Spot](https://docs.cloud.google.com/compute/docs/instances/spot), and [stop/suspend semantics](https://docs.cloud.google.com/compute/docs/instances/suspend-stop-reset-instances-overview): use bounded runtime, expect interruption and account for retained attached resources.
- [Canonical Ubuntu 26.04 release notes](https://documentation.ubuntu.com/release-notes/26.04/): 26.04 is an LTS release. A suitable official image at the selected provider/region and stack compatibility remain unverified.

Routed model work, economic break-even and provider choice in architecture sections 14–15 are design inferences from these constraints, not provider performance guarantees. No provider account, current rate quote, endpoint, cloud instance or billing state was queried.
