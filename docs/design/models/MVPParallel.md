# MVP parallel admission evidence

This is design evidence for architecture A33, not runtime implementation or permission to launch workers. The thin MVPParallel module extends the existing Admission model without changing its transitions.

The English invariants are: reservations stay within capacity; an action is redeemed at most once; reservations retain their owner and cover redeemed work; redemption checks current controls and generation. A separate deliberately false SerialOnly invariant checks that the model can actually reach two simultaneously redeemed independent tasks.

On 2026-10-08, SANY parsed MVPParallel successfully. TLC 2.19 exhaustive breadth-first checking with two workers, two actions, capacity two and central generation 0–2 completed with no invariant violation: 5,873 generated states, 1,820 distinct states, zero queued states, depth 13, exit 0. Unlike the earlier restricted run documented in README.md, this session allowed the checker to initialize its listener.

The witness configuration failed SerialOnly as intended, exit 12. Its seven-state trace checks/reserves/redeems a1 for w1, then checks/reserves/redeems a2 for w2. Both workers are redeemed with distinct owners and two held reservations. This establishes reachable overlap in the abstract model, not a fairness or throughput guarantee.

The existing Admission-broken-reserve configuration also failed as intended, exit 12, at depth 5. Both workers first observed a free slot at capacity one; stale observations then allowed both reservations. The trace confirms that capacity must be checked and reserved atomically, rather than with a separate eligibility read.

Reproduce from this directory:

~~~sh
rtk proxy sany MVPParallel.tla
rtk proxy tlc -workers 1 -metadir /tmp/skybuild-mvp-parallel-check -config MVPParallel.cfg MVPParallel.tla
rtk proxy tlc -workers 1 -metadir /tmp/skybuild-mvp-parallel-witness -config MVPParallel-witness.cfg MVPParallel.tla
rtk proxy tlc -workers 1 -metadir /tmp/skybuild-mvp-reserve-negative -config Admission-broken-reserve.cfg Admission.tla
~~~

The last two commands are expected to fail. Checker scratch files belong outside the repository.

The Store implementation must realize Reserve and Redeem as atomic validated transactions. Unknown completion cannot release capacity, and an eligibility response cannot authorize launch. This model has only one attempt per worker and inherits the limitations in [README.md](README.md): no physical spawn/crash journal, local-generation replay, deadline/accounting, provider cancellation, publication or database recovery proof. Those boundaries still require their own selected implementation contracts and tests. No liveness claim is made.
