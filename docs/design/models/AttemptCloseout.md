# Attempt closeout bounded model

`AttemptCloseout.tla` models one immutable task/attempt/fence/source invocation,
cached outage policy, an approval cutoff and finite closeout grace. New task
admission and ordinary task calls end at cutoff; only closeout calls remain
available through grace. The `finish-current-task` policy permits the pinned
current task to continue during a contact outage before cutoff. It covers
reconnect, current-clock observation, restart invalidation, clock
parking/reconciliation, checkpoint summaries,
operator stop request/observation, process uncertainty and exposure release.
`JournalRoom` bounds modeled journal events at eight so TLC explores a finite
state space; it is a model bound, not a production journal limit. To control
the state count, the model stores this event count instead of event payloads,
stores only the presence/time of the one pinned-task admission (the task ID is
fixed structurally), and uses
monotonic safety-monitor flags instead of an append-only call history. TLC
checks invariants in every reachable state, so the flags preserve detection of
any task/closeout window, parked-clock, or missing-current-clock violation.
This abstraction intentionally omits journal content/hash behavior and call
ordering/multiplicity; those remain source-test concerns. The mutation still
emits a concrete after-grace task call and must violate `CallsRespectWindow`.

Run `tlc` against `AttemptCloseout.cfg` for the bounded safety check. Run
`AttemptCloseout-broken.cfg` as a mutation: `BrokenLateTaskCall` deliberately
allows an ordinary task call after grace, and `CallsRespectWindow` must produce
a counterexample. The model is not evidence that an OS process was physically
stopped or that the caller's server-reconciliation evidence was authenticated.

At commit `9dab055975b5374692701dccfd5f3aef3537f08a`, the safe bounded TLC run
completed with 9,074 distinct states in 4 seconds. The deliberate mutation did
not reach its intended invariant counterexample: TLC exited 75 on an
incomplete primed Boolean assignment (`badCallWindow = null`). This was a
model-expression bug, not a passing mutation check. The three safety-monitor
primed Boolean updates in all three call actions are now parenthesized, and a
focused source preflight checks all nine expressions. The corrected exact head
still requires fresh SANY, safe TLC, and mutation runs.

The first exploratory trace exposed an important stale-observation issue: an
unqualified `unknown` sample after the exact invocation had been observed
exited could make terminal evidence appear to regress. The model now allows
uncertainty only before terminal evidence for that pinned invocation. A
foreign invocation is modeled separately and retains exposure/conflict rather
than changing the pinned process's terminal state. The source reducer applies
the same rule and rejects an exact-invocation `running`/`unknown` sample after
an `exited` sample.

The local file lock, private file modes, fsync/rename protocol and hash chain
are source-level journal properties, outside this small state model. The
journal protects against accidental corruption and concurrent processes; it
does not establish a security boundary against another process running as the
same UID.
