# Bundled integration source follow-up

Date: 2026-10-08. Read-only current on-disk SkyKeep inspection through this session’s CodeGraph after `codegraph status` confirmed `/home/kevin/my_code/skykeep`. SkyBuild has no index. This is design evidence, not live service or test qualification.

Source HEAD: `383d3d375979c66b39df0f61c19a165957fecdcf`. CodeGraph reported 1,285 files and 191 pending additions; exact file hashes below identify the inspected content independently of index freshness or checkout dirtiness. No index was created or synchronized.

- `scripts/todo_service/integration.py`, SHA-256 `e1922d4c83ec3be1c052ae82334c7c2545ffb2591e0d2acb13defbc9b23537d1`: `scan`, `create_set`, `move_set` and `document` provide source readiness and multi-member integration-set state, targets, profiles and merged-result metadata. Legacy identifiers retain their source spelling.
- `scripts/todo_service/integrate_set.py`, SHA-256 `090fb1f35a1a8a8bc73cd9ef1642105561647e3a735e5d2a4a9a42d4d752632d`: the helper explicitly handles only the light profile, uses a detached integration worktree, excludes conflicting members, pushes its result, and reports owner-operated PR closure. Its header states that it skips the main lane/gate checks and refuses full-profile integration.

Reusable ideas are integration-set membership/readiness, target/profile/result metadata and an isolated combined worktree. The new contract additionally requires frozen member heads/base/policy, combined gates, strongest applicable profile, confirmed automatic publication and per-task inclusion, uncertainty reconciliation and bounded failure diagnosis. The legacy light runner cannot be adopted unchanged as this automatic merge path.

The owner’s hour-long suite/twenty-task example motivates backlog coalescing, not a measured throughput claim. Prepare the next bundle during the active gate without mutating its candidate. Core bundling belongs to SKYBUILD-BUNDLED-INTEGRATION; cloud capacity optimization remains deferred. See [architecture](../architecture.md) and [ADR 0028](../../adr/integration.md#adr-0028).
