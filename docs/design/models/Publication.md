# Publication design model

Planning evidence for architecture A30 and [ADR 0030](../../adr/integration.md#adr-0030). The [spec](Publication.tla) models two bundles, target versions 0–3 and ownership generations 0–1. It does not run Git or contact GitHub.

## Invariants and boundaries

1. Every remote publication uses the same base that its candidate was gated against.
2. Dispatch validates the current ownership generation.
3. Every dispatched but unreconciled operation retains the publication slot; at most one slot is held.

Gate captures candidate base/generation. Dispatch rechecks local state. Another writer can then change the target before RemoteApply. The remote guard rejects that changed base. Remote outcome and local reconciliation are separate: an applied request can remain unacknowledged, and owner replacement does not release its slot. Reconcile releases only after observing a conclusive outcome.

## Actual validation, 2026-10-08

SANY parsed/semantically processed Publication.tla successfully. TLC 2.19 random simulation exited 0 after 10,000 traces of depth 40, seed 20261008, one worker; it reported 400,001 sampled states checked. No invariant violation was found. Exhaustive checking was not run for this model; earlier exhaustive TLC attempts in this sandbox failed during RMI listener initialization, as recorded in [admission notes](README.md).

Each deliberate mutation exited 12 with the intended invariant violation. Traces were read:

| Configuration | Observed counterexample |
| --- | --- |
| Publication-broken-remote.cfg | Gate and dispatch against base 0; another writer advances target to 1; remote publication accepts base-0 evidence against base 1. Ownership replacement also occurs in the sampled trace but does not cause the base mismatch. |
| Publication-broken-fence.cfg | Gate under generation 0, replace owner with generation 1, then dispatch the old generation. |
| Publication-broken-release.cfg | Dispatch and retain pending remote outcome, then clear the slot without reconciliation. |

## Reproduce

From this directory, using the installed tools:

~~~sh
rtk proxy sany Publication.tla
rtk proxy /home/kevin/.local/opt/jdk/bin/java -Xmx256m -XX:+UseParallelGC -XX:-UsePerfData \
  -cp /home/kevin/.local/opt/tlaplus/tla2tools.jar tlc2.TLC \
  -simulate num=10000 -depth 40 -seed 20261008 -workers 1 \
  -metadir /tmp/skybuild-publication-simulation -config Publication.cfg Publication.tla
~~~

Repeat with the three mutation configurations and distinct scratch metadirs; each must fail. Scratch output from this audit was recorded under `/tmp/skybuild-a30-Publication*.log`; durable evidence is the spec, configurations and this result/trace account, not an assumption that temporary logs survive.

## Limits and implementation obligations

The remote base guard is an assumed atomic boundary that the chosen GitHub flow must actually supply; a local comparison or PR-head-only check does not satisfy it. Gate is abstract: test completeness, union of requirements, review quality, source/environment/policy provenance, authentication and candidate isolation are not checked by this model. Dispatch combines durable intent and send into an abstract step; actual local crash/send gaps still require the effect journal and qualified publisher.

No liveness, fairness, deadlock-freedom or eventual remote response is claimed. Idle permits stuttering. The small target bound limits sequences, and each candidate is prepared/dispatched at most once. No budget, stop cancellation, restore, hash collision/ABA, actual Git graph or network protocol is modeled. Already-dispatched work may complete after a generation change; the model deliberately does not assert an instantaneous physical stop. Simulation is sampled evidence, not exhaustive proof or implementation validation.
