# Overlapped bundle preparation

Bounded follow-up `SKYBUILD-OVERLAPPED-BUNDLE-PREPARATION`, source base
`b066ecaa6f07ef20a84ad7936bbf6c876dc9aa7d`. Marshall run001 waited through
another bundle's full gate because integration retained the worktree capacity
lock. This implements the accepted preparation pipeline without changing task
authority, gate requirements, frozen membership, or publication acceptance.

## Locking contract

Cooperating integration helpers hold `skybuild-integration.lock` in the common
Git directory from GitHub/ref observation and fetch through candidate creation, the gate,
publication checks, and temporary candidate cleanup. After acquiring this lock,
recheck PR state and remote base/head before creating or testing a candidate.
A second integrator waits; stale inputs then fail before another full gate.

The shared capacity lock covers worktree counting and Git registration only.
Once registered, the candidate remains counted by later reservations while its
gate runs. Existing preparation uses this same short creation section and
requires two available slots; integration requires one. The limit remains 64.
The lock order is publication then capacity; preparation never waits for the
publication lock. Cleanup can only reduce the count. A crashed process releases
its advisory locks, while retained Git worktree records continue counting.

These are repository-local advisory locks for cooperating tools. Direct Git
operations, another clone/machine, GitHub writers, external acknowledgment, and
atomic expected-base publication remain outside this contract. The existing
remote and published-tree checks remain required. There is no automatic retry,
worker launch, failed-candidate deletion, or gate weakening.

## Bounded model and implementation checks

`models/BundlePreparation.tla` models two preparers, two integrators, registered
worktrees, publication ownership, completion, and crashes that release ownership
while preserving worktree records. Atomic count-and-registration represents the
capacity critical section. The normal configuration uses baseline one and limit
three. TLC checked 360 distinct states without violating capacity or publication
exclusion. This is bounded design evidence, not a proof of Python or GitHub.
Crashes occur between modeled atomic steps. Parent death during Git registration,
orphan Git subprocesses, partial filesystem writes, and forced lock-file
replacement are outside this model; ambiguous creation still needs reconciliation.

The broken capacity configuration separates observation from registration; its
trace admits two stale preparer observations and an integrator, producing four
worktrees against a limit of three. The broken publication configuration admits
two holders. Both must fail. A separate overlap witness uses limit four and an
intentionally false no-overlap invariant to show prepared and gating candidates
can coexist. This witness is reachability evidence, not a liveness guarantee.

Run from the model directory with a private external state directory:

```sh
JAVA_TOOL_OPTIONS=-Xmx512m sany BundlePreparation.tla
JAVA_TOOL_OPTIONS=-Xmx512m tlc -workers 1 -deadlock \
  -metadir /absolute/scratch/tlc-states BundlePreparation.tla
```

Use `-config` for each broken/witness configuration and retain its expected
counterexample. The normal check must pass; the three negative/witness checks
must report their named invariant violation.

Focused regressions exercise the actual integration call site: a new preparer
acquires capacity after the fake gate starts, while real advisory publication
locking keeps a second integrator waiting. Separate tests cover insufficient
capacity, count/creation serialization, and a base move while waiting that
creates no candidate and runs no gate. Full combined tests and independent
exact-head and frozen-candidate review remain required before publication.
