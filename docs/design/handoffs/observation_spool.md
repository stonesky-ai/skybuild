# Observation spool implementation handoff

Task: `SKYBUILD-EXECUTION-CONTROLS`. Owner: main SkyBuild session. Phase: awaiting independent review and combined validation. Source base: `257216c5498b95d5a6e56bca60c31abcd6736226`. Branch: `task/observation-spool`.

This slice implements only local observation delivery. It does not launch or inspect processes, infer death, release reservations, complete tasks, change projections locally, or modify Store, API, or schema. The next action is independent exact-head review followed by the parent's frozen combined PostgreSQL gate. Deployment and controller writes remain outside scope.

## Sources

At the source base, `src/skybuild/observations.py:11` (`Observations.record_observation`) defines the exact packet fields, reserved attempt/fence checks, event-ID and per-identity sequence idempotency, and append-only response. Its process identity includes task, attempt, fence, component, source, boot ID, PID, and process start. The spool preserves these fields without generating identities or source sequences. `tests/test_observations.py:38` demonstrates identity pinning across reordered delivery and process changes.

Architecture sections 8–10 require durable replay during outages and separate evidence from execution authority. SkyKeep source inspected at `383d3d375979c66b39df0f61c19a165957fecdcf`, `scripts/agents/session_keeper.py:1306` (`lock_holder_pids`) and its `:1347` diagnostic, provides a narrower PID/lock ownership pattern. PID-only diagnostics cannot establish observation identity. No Keeper code is copied; the existing SkyBuild compound identity remains authoritative.

## Interface and assumptions

Explicitly create an ordinary user-owned mode-0700 absolute directory outside transient worktrees. Construct `ObservationSpool(directory)`, call `enqueue(project_id, packet)`, then `replay(transport, limit=...)`. `pending()` returns bounded snapshots including sanitized durable last outcomes. The injected synchronous transport takes `(project_id, packet)`, must enforce a finite timeout and correctly configured authentication, and returns the exact `record_observation` event or raises an error. This library supplies no polling process, HTTP route, credentials, or launcher.

All cooperating callers share the directory lock. Record and lock files are mode 0600, user-owned, regular, and not hard links. Directory and file opens reject final symlinks; the caller must keep parent paths stable and trusted. Unexpected, corrupt, oversized, or insecure files stop delivery for reconciliation. Unpublished temporary files left by crashes are discarded under the lock and never sent.

Publication handles short writes, fsyncs temporary contents, renames atomically, and fsyncs the directory. Replay reaffirms directory durability before transport. A lost response preserves the original packet across restart. A matching response must identify the project, event, full process identity, source sequence, normalized observation timestamp, state, and evidence references. Only then does replay unlink and fsync the directory. A crash around acknowledgment can cause another identical replay; server idempotency handles that uncertainty.

Unknown/conflicting responses never remove or rekey a packet. Replay returns the original response/error and persists only status and optional HTTP/domain status code. Exception strings and remote bodies are never written to disk. Observations with `unknown`, `exited`, `interrupted`, or `result-reported` state remain evidence, without task completion or capacity release.

Defaults: 256 committed records, 1 MiB admitted committed storage, 16 KiB per record including reserved metadata, and at most 32 replay attempts per call. Admission reserves 128 bytes for bounded outcome metadata. Atomic publication can temporarily consume one additional record of at most the configured record size. Lowering bounds below existing contents requires reconciliation. Full capacity refuses new evidence without overwriting pending records. Replay holds the directory lock during transport, so caller timeout discipline is required. There is no automatic expiry or conflict eviction.

## Validation and remaining acceptance

Focused command: `PYTHONPATH=src /home/kevin/my_code/skybuild/.venv/bin/python -m pytest -q tests/test_observation_spool.py`. Result before commit: 35 passed, 1 skipped. The skipped test requires the task-owned `SKYBUILD_TEST_DSN` from the parent's combined gate. It checks actual `record_observation` replay after a lost reply, one durable event, and unchanged held claim/reservation.

Unit tests cover interrupted writes and fsync/rename failures, short writes, stale temporary files, accepted-but-lost replies, restart, API unavailability, conflicts, changed boot/PID/start identity, conservative acknowledgment, private paths, corruption/symlinks, record/byte/packet/replay bounds, and concurrent duplicate enqueue.

A two-event scratch TLA+ model checked durable-before-send, retained-until-accepted, matching acknowledgment, and bounded pending records: 35 distinct states, no error. Removing the acceptance precondition yields a four-state counterexample dropping published evidence before acceptance. Scratch artifacts: `/tmp/skybuild-observation-spool-model`. This bounded design evidence assumes atomic lock-protected publication/removal and immutable event identities; filesystem failure tests exercise those implementation boundaries. Independent review and the full combined gate remain required.
