# Launch-free CPU dispatch simulator (historical model)

This document records the earlier fake-adapter tranche. The same directory now
also contains `CPUWorkerRelay.tla`, which models the production CPU worker
settlement and owner-relay ordering. The simulator below remains useful for its
older transaction and negative-control evidence; it is not the current real
dispatch contract.

Task: `SKYBUILD-EXECUTION-CONTROLS`. Source base: `257216c5498b95d5a6e56bca60c31abcd6736226`.

The historical `CPUDispatch` implementation exercises the existing reservation/effect contract without launching a process. It adds no worker loop, callback, systemd call, or network transport. `reserve_cpu` remains unredeemable by this simulator. Every simulator response says `physical_dispatch_authorized: false`.

## Safety contract

The invariants selected before implementation are:

1. A CPU action and attempt retain one operation and one process identity across retries and coordinator restarts.
2. Uncertain dispatch and unacknowledged stop retain both effect exposure and capacity. Claim expiry, changed controls, and observation reports cannot release either.
3. A new simulated start revalidates the reservation's task revision, readiness generation, claim holder/fence/lease, and both restrictive control generations.
4. Stop intent prevents a delayed start. A stop acknowledgment before start creates a permanent terminal tombstone; it cannot later become a running process.
5. Only the simulator's terminal receipt for that exact operation, process identity, and reservation binding settles its fake effect and reservation. It does not accept task completion, release a claim, or settle an ordinary effect.

The reservation's immutable `intent_hash` binds project, task, actor, action, attempt, units, task revision, readiness generation, claim fence, central generation, and local generation. The fake effect additionally binds the input digest, fixed fake policy digest, operation ID, and allocation reference. `adapter_kind = cpu-fake-v1` is immutable and cannot be attached to an existing unqualified effect. Fake `authority_epoch = 1` is a simulator namespace, not a restored or live execution epoch.

## Transactions and replay

`prepare_fake` revalidates admission and commits an `unknown` fake effect, immutable dispatch identity, and journal entries in one transaction. It returns only after commit. A repeated action must carry the same operation and input digest; it returns the current state, including after settlement. The original reservation remains held.

`start_fake` uses a second transaction to simulate the adapter. It revalidates current authority and all reservation bindings before inserting the one durable running receipt. An existing receipt makes retries observational. A new coordinator can therefore recover the same identity after losing the start response. There is no external call inside either transaction, and no extensibility hook can substitute a real launcher.

`request_fake_stop` commits a stop tombstone without releasing capacity. `acknowledge_fake_stop` is a separate simulator transaction: it records terminal proof for the same identity, including a zero-start tombstone when stop won the race. Losing either response changes no identity and releases nothing. These two methods and reconciliation remain available to the owner after a claim expires or a restrictive control changes.

`reconcile_fake` reads the simulator receipt rather than accepting a caller-supplied report. It atomically settles the fake dispatch/effect and releases only the bound CPU reservation, with effect and CPU journal entries. Missing or running receipts leave exposure held. Observation events, including `exited`, remain evidence only. Ordinary unknown effects retain their existing irreversible state; the generic effect observation API cannot request settlement.

All transitions use the existing project graph lock followed by the CPU pool lock. Preparation also serializes global action and operation identities. Claims, readiness, dependency changes, and controls already use the corresponding locks. The SQL triggers preserve identities, prevent receipt resurrection, and require matching terminal proof before release. Failed journal insertion rolls the entire transition back. The restricted runtime role receives only the same explicit table privileges used by the existing launch-free Store; candidate processes must never receive that database credential.

## Bounded model evidence

`CpuDispatch.tla` models two operations sharing capacity one, one task/claim, at most two control changes, one revision invalidation, and one claim expiry. Preparation and simulator receipts are separate atomic steps. Missing replies and coordinator restarts are represented by durable state remaining unchanged; duplicate requests cannot add another `Start` transition. Observation and terminal acknowledgment are separate actions. `Start` and `StopRequest` may occur in either order.

SANY parsed the model. TLC 2.19, JDK 21, two workers, 512 MiB heap, exhaustively checked **13,797 generated states / 5,884 distinct states**, depth **19**, with no invariant violation. There is no fairness, liveness, deadlock-freedom, or physical exactly-once claim. Deadlock checking is explicitly disabled because terminal stuttering is allowed. There is no state constraint excluding failures.

The negative control sets `BrokenRelease = TRUE`, allowing an observation to release a reservation. TLC found `TerminalProof` violated at depth five after 107 generated / 82 distinct states: `Init`, `Reserve(a)`, `Prepare(a)`, `Observe(a)`, `Reconcile(a)`. The final counterexample state had `phase[a] = "settled"`, `adapter[a] = "absent"`, `starts[a] = 0`, `observed = {a}`, and `held = {}`. Thus the adapter was still absent while the reservation was released. This checks that terminal-proof protection is reachable and meaningful; it is not a proof of every implementation detail.

Reproduce with the installed wrappers or an explicitly bounded Java heap:

```sh
sany docs/design/models/cpu_dispatch/CpuDispatch.tla
tlc -workers 2 -metadir /tmp/cpu-dispatch-tlc docs/design/models/cpu_dispatch/CpuDispatch.tla
sed 's/BrokenRelease = FALSE/BrokenRelease = TRUE/' \
  docs/design/models/cpu_dispatch/CpuDispatch.cfg > /tmp/cpu-dispatch-broken.cfg
tlc -workers 2 -metadir /tmp/cpu-dispatch-tlc-broken \
  -config /tmp/cpu-dispatch-broken.cfg docs/design/models/cpu_dispatch/CpuDispatch.tla
```

PostgreSQL tests separately exercise concurrent retries, start/stop ordering, lost acknowledgments, restart recovery, each invalidated admission binding, observation-only reports, generic unknown effects, forged identity, journal failure rollback, and the restricted runtime role. The model abstracts exact hashes, per-host identity, lease clocks, authentication, database recovery, and adapter transport. Real deployment needs independent evidence for each of those boundaries.

Scoped validation on 2026-10-09 passed 89 tests across `test_cpu_dispatch.py`, `test_admission.py`, and `test_runtime_role.py`, then 28 compatibility tests across `test_effects.py`, `test_claims.py`, and `test_observations.py`. Both gates used newly created disposable PostgreSQL databases, required 10 GiB available host memory, and reported successful container cleanup. The complete combined bundle gate and independent exact-head review remain integration requirements.

## Required physical acknowledgment contract before any real adapter

The simulated adapter does not authorize real execution. Before any real CPU canary, a separately reviewed adapter must satisfy all of the following:

- Bind a protected local journal to controller authority epoch, project/task/action/attempt/operation, claim fence, revision/readiness/control generations, input/policy digests, allocation, host identity, boot identity, and an execution identity that cannot alias a recycled PID or unit name.
- Durably record acceptance before issuing an OS start. Replays inspect that same journal and process identity; they cannot create another attempt, reset resource accounting, or reuse a terminal operation.
- Define the OS start ambiguity explicitly. A lost `systemd-run` response is uncertain. Reconcile the journal against the exact unit/cgroup and process start identity; neither a missing response nor an unloaded unit alone proves that execution never began.
- Serialize local start and stop handling. Persist a stop tombstone, fence all delayed start requests, and reconcile in-flight starts before acknowledging quiescence. An acknowledgment must establish either that the operation never started and cannot subsequently start, or that its entire execution boundary has terminated and cannot restart.
- Return authenticated, replay-safe terminal proof from the trusted supervisor, bound to the full operation and execution identity and monotonic local journal generation. An observer report, exit code, `systemctl stop` success, or lease expiry alone is insufficient.
- Retain exposure and reservations during disconnects, uncertain starts/stops, lost proof, or controller restore. A restored controller requires a new authority epoch and reconciliation of surviving execution; restoring data cannot revive old permits.
- Keep command execution and trusted proof credentials outside candidate code. Qualify bounded timeouts, child-process containment, restart policies, local restrictions, shutdown/reboot, and every existing launch bypass. Prove the physical behavior independently of the database model.

Reuse evidence: SkyKeep commit `383d3d375979c66b39df0f61c19a165957fecdcf`, `scripts/agents/session_keeper.py`, `SystemdSpawner` at lines 2112–2167 provides bounded subprocess calls, unit-state inspection, and stop/start separation. Its `UnitState` at lines 2098–2105 and `state()` at lines 2130–2146 distinguish absent, active, and ended units. Those are useful adapter mechanics, not a durable operation ledger or proof that every child terminated. The drain policy remains the existing SkyBuild architecture section 8 / ADR 0018: fence new work while preserving the lifecycle of already-authorized CPU work. No legacy implicit restart or escalation behavior is imported.

## Current bridge source tranche

`CPUWorkerDispatch` and `cpu_worker_bridge.py` add the real selected-profile
path. The controller reserves against the exact live worker claim, commits a
held dispatch/effect before systemd I/O, pins host/unit/nonce/InvocationID, and
settles only from an exact successful terminal observation plus immutable
worker result intent and authenticated remote head. The worker exits before
task submission. After settlement, the owner controller relays the original
fenced result through existing Cord and workflow idempotency keys. Unknown
launches or terminal states retain exposure. No stop request is treated as
physical termination.

The controller rechecks the shared unresolved-usage helper under the graph lock
at begin; if that source is absent, begin fails closed. Production qualification
requires the combined migrations 014/015/016 and source bundle. The new
`CPUWorkerRelay.tla` model is prepared but remains unchecked pending the
serialized remote TLC plan. PostgreSQL execution, full combined bundle gates,
published integration, and actual systemd qualification remain pending.
