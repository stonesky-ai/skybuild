# Admission design model

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
