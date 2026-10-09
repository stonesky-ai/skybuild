# Admission design model

## Launch-free observation receipt model

[ObservationJournal.tla](ObservationJournal.tla) and [ObservationJournal.cfg](ObservationJournal.cfg) check pinned identity, increasing source sequence, replay and crash/reconnect. The [implementation brief](../implementation/observation-journal.md) records the bounded results, deliberate identity mutation and transaction assumptions. This model grants no execution or completion authority and does not prove durable offline host spooling.

Reproduce from the repository root:

~~~sh
rtk proxy sany docs/design/models/ObservationJournal.tla
rtk proxy tlc -workers 2 -metadir /tmp/skybuild-observation-check docs/design/models/ObservationJournal.tla
~~~

## Store-only CPU reservation slice

`CPUReservation.tla` and `CPUReservation.cfg` model the unredeemable Store slice,
separately from the future launch/redemption model below. English invariants:
held unit reservations never exceed capacity; cancelled action identities never
revive; unknown exposure retains its reservation; new reservations require current
central/local generations, readiness/task revision and a live fenced claim.
Expiry changes claim liveness without releasing capacity. Only explicit
never-dispatched cancellation releases capacity. There is no dispatch operation.

On 2026-10-08 SANY passed. TLC 2.19 exhaustive breadth-first checking with two
actions, unit capacity one and generations/revision 1–2 passed all five invariants:
6,985 generated states, 2,236 distinct states, depth 12. This is bounded safety
evidence, not a liveness, physical-launch or implementation proof.

Three negative controls were checked by copying the config to `/tmp` and changing
exactly one `Broken*` constant to `TRUE`. `BrokenCapacity` produced two held actions
at capacity one. `BrokenReplay` produced Check, Reserve, Cancel, Replay and violated
`NoRevival`. `BrokenFence` admitted under a disabled central control and violated
`NoStaleAdmission`. Each finished with invariant violation and exit 12. The first
fence mutation exposed missing parentheses in a Boolean assignment; that model
expression was corrected before the final positive and mutation checks.

Reproduce from the repository root:

~~~sh
rtk proxy sany docs/design/models/CPUReservation.tla
rtk proxy tlc -workers 2 -metadir /tmp/skybuild-cpu-reservation-check docs/design/models/CPUReservation.tla
~~~

Implementation mapping: graph lock serializes task/readiness/dependency changes;
project CPU lock serializes controls, capacity and cancellation; global action and
attempt locks serialize identity registration across projects. Task and claim row
locks follow those locks. Reservation insertion repeats the live-lease predicate
using `clock_timestamp()` after all checks. State and journal commit together.
An idempotent action lookup returns current stored state, never cached admission.
The model abstracts one immutable action and fixed claim identity per task, unit
requests, and one revision representing task/readiness invalidation. Claim fence
replacement is covered separately by `Claims.tla` and PostgreSQL checks. PostgreSQL
tests additionally cover integer requests, identities, authorization, rollback and
lease expiry during checks. No fairness, wall-clock model, process launch, provider
meter, complete scoped-control hierarchy or DB-outage behavior is proved.

Sources: architecture A34 section 8 and implementation plan section 5 at base
`a454f09bcd5a634445790ad0df7ab8ddebf58ccf`. SHA-256:

- architecture.md: `f4f2c2cbe93132a36177aa6baf6728c4a207af8f0254770c6b3e80f605533f85`
- implementation_plan.md: `77e65bfaa6b0f64a4a44d9f069dca87a9936a88f062bcbe90d30461c74367260`
- Admission.tla: `d362656e3755b8e9c88a918485da47f5e33ae4061b3f90b7928142813c458767`
- Claims.tla: `d3a02dd2b15e20adb2c7cae93d9ad09975506e98e80ee72a729dd820f7442802`

[MVPParallel.md](MVPParallel.md) records a later exhaustive two-worker/capacity-two check, a reachable parallel-work witness and the stale-capacity negative control. It does not extend the modeled physical-launch guarantees.

For the separate A30 bundle publication audit, see [Publication.md](Publication.md). Its sampled results and external-enforcement assumptions are distinct from the admission model below.

[Reassessment.md](Reassessment.md) checks a task input change arriving during readiness evaluation, with a deliberate lost-invalidation mutation. It also provides bounded simulation evidence only.

This is a small planning specification, not SkyBuild runtime code. It examines transaction-level admission assumptions from architecture section 8. It does not implement a launcher or authorize any launch.

## English invariants

1. Reservations never exceed the configured slot capacity.
2. One action identity is redeemed at most once, even when different workers request it.
3. A reserved attempt retains its action owner and a redeemed run retains its reservation until terminal observation.
4. Redemption requires current central enable, local enable and control generation. A prior capacity observation is not an atomic reservation.

The model separates Check, Reserve, Redeem and Terminal. Central/local control can interleave between steps. Central generation is bounded and local enable can toggle; an already redeemed attempt may continue after stop while it reaches its bounded checkpoint. Thus “every running process implies enabled” is deliberately not an invariant.

## Actual validation on 2026-10-08

- SANY parsed and semantically processed Admission.tla successfully.
- TLC 2.19 exhaustive breadth-first checking could not start: its fingerprint implementation attempted an RMI listener and the sandbox returned “Operation not permitted.” No exhaustive pass is claimed. A depth-first attempt also did not complete and is not evidence.
- TLC random simulation completed with no invariant violation: two workers, two actions, capacity one, central generation 0–2, 10,000 traces of depth 30, seed 20261008, one worker thread; output reported 300,001 states checked. These are sampled states, not distinct-state exhaustive coverage.
- Both deliberate mutations failed with invariant violations (exit 12), providing a limited vacuity check. BrokenReserve uses stale observed capacity: both workers check an empty slot, then reserve different actions, leaving two held reservations at capacity one. BrokenFence skips redemption controls: a worker reserves, its local enable becomes false, then redemption sets invalidRedemption. Trace inspection confirmed these are the intended failures.

The first draft had an incorrectly grouped Boolean expression that made one mutation's successor incomplete. It was corrected and SANY plus all three simulations were rerun. The results above describe only the final spec.

## Reproduce

From this directory, with the owner's installed tools:

~~~sh
rtk proxy sany Admission.tla
rtk proxy tlc -workers 1 -metadir /tmp/skybuild-admission-check -config Admission.cfg Admission.tla
~~~

The second command requires an environment where the standard TLC implementation can initialize its local listener. Within this restricted session, the successful fallback used the installed Java and jar directly with a bounded heap:

~~~sh
rtk proxy /home/kevin/.local/opt/jdk/bin/java -Xmx512m -XX:+UseParallelGC -XX:-UsePerfData \
  -cp /home/kevin/.local/opt/tlaplus/tla2tools.jar tlc2.TLC \
  -simulate num=10000 -depth 30 -seed 20261008 -workers 1 \
  -metadir /tmp/skybuild-admission-simulation -config Admission.cfg Admission.tla
~~~

Repeat the simulation with Admission-broken-reserve.cfg and Admission-broken-fence.cfg; each is expected to fail. Use a different scratch metadir for each simultaneous run. Checker state belongs under /tmp, not the repository. The spec/configs are retained here so future validation can run without reconstructing them.

## Assumptions and outstanding design work

Reserve and Redeem are atomic database steps; implementation needs matching transaction isolation/constraints. Action ownership is retained forever in this bounded one-attempt-per-worker model; retry attempts need explicit new identities and a reconciled previous terminal state. Capacity is a unit-slot proxy, not money, tokens or memory. Terminal means proven completion, not timeout or missing heartbeat. Central generation models stale control; durable local generations, permit expiry, clock skew, crashes/journals, cancellation and fencing external side effects are not modeled.

There is no fairness or liveness claim. Local toggle always provides a successor, so this model does not prove deadlock freedom or useful progress. There is no OS spawn step, provider, database outage, partitioned stop acknowledgment or orphaned cloud resource. A database redemption cannot prove physical single execution. Extend the spec for the selected local launch journal and stop acknowledgment protocol before treating those guarantees as ready for implementation. Run exhaustive checks outside this sandbox when possible, but do not confuse a larger model with implementation verification.
