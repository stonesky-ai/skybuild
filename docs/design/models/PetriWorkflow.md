# Bounded Petri workflow check

The model checks the design of atomic task transitions. It does not prove the Python or PostgreSQL implementation.

Safety requirements:

- Each task has exactly one token in one of the seven places.
- A worker can submit only with the current owner and claim fence.
- A check pass cannot reverse a confirmed failure in the same attempt.
- An unknown publication keeps the token in Integrating.
- A pending Hold or Deferred request prevents claim, freeze and publication dispatch.

The model uses two workers and at most two claims. It has one task and one unresolved external effect.
Atomic steps represent a Store transaction under the project graph lock and task row lock.
The claim counter never decreases. Old worker snapshots remain available after expiry.
A new claim starts a new attempt and clears the modeled attempt failure.
The model omits authorization, evidence content, SQL behavior, remote response validity, retries and liveness.
The Python and PostgreSQL checks must cover those requirements separately.

Use the existing `Reassessment.tla` and `Publication.tla` models for concurrent input acknowledgments and external expected-base publication.
This model adds only the Petri place, pending control, claim fence and check-order requirements.

Run SANY first. Run TLC with `JAVA_TOOL_OPTIONS=-Xmx256m`, `-workers 1` and a private `-metadir`.
The `-deadlock` option disables deadlock checking because this model checks safety and permits terminal states.

The normal configuration passes: 571 states generated, 285 distinct states, depth 10.
The broken fence configuration fails `CurrentFenceWrites` after claim, expiry, replacement claim and stale submission.
The broken result configuration fails `FailureCannotAdvance` after submission, failure and late pass.
Both broken checks exit with status 12 and reach the expected failure at depth 5.
These checks establish that the bounded model exposes the two intended defects.
