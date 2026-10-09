# Reassessment design model

Planning evidence for architecture A30 and [task workflow](../task_workflow.md). [Reassessment.tla](Reassessment.tla) checks the dirty-generation race: input changes while a readiness evaluation is running must not be acknowledged as already processed.

The model has source revisions 0–2, one evaluation and a captured input revision. A successful finish can publish/acknowledge only when its captured revision still matches. Otherwise the prior assessment remains stale and another evaluation is needed. This abstracts the Store transaction/conditional update; it does not implement a worker.

SANY passed. TLC 2.19 simulation exited 0 for 10,000 traces of depth 20, seed 20261008, one worker, reporting 200,001 sampled states checked without an invariant violation. The broken-ack mutation exited 12 with `NoFalseFresh` violated. Its trace begins evaluation at revision 0, changes inputs to revisions 1 then 2, and incorrectly marks revision 2 assessed using the revision-0 projection. The trace was read and matches the intended lost-invalidation failure.

Reproduce from this directory:

~~~sh
rtk proxy sany Reassessment.tla
rtk proxy /home/kevin/.local/opt/jdk/bin/java -Xmx256m -XX:+UseParallelGC -XX:-UsePerfData \
  -cp /home/kevin/.local/opt/tlaplus/tla2tools.jar tlc2.TLC \
  -simulate num=10000 -depth 20 -seed 20261008 -workers 1 \
  -metadir /tmp/skybuild-reassessment-simulation -config Reassessment.cfg Reassessment.tla
~~~

Repeat with Reassessment-broken-ack.cfg and a distinct scratch metadir; expect an invariant failure. Audit logs were `/tmp/skybuild-a30-Reassessment*.log`; the specification, configurations and these recorded results are the durable evidence.

This is sampled, bounded evidence. Exhaustive checking was not run; the earlier sandbox listener restriction is recorded in [admission notes](README.md). No liveness/fairness claim is made; Idle permits stuttering. The model assumes atomic change/finish and one claimed evaluator. It does not check task transition semantics, multi-task dependency propagation, authorization, journal immutability, real database isolation or external effects. Those remain implementation acceptance cases. A correct generation check cannot compensate for a mutation path that fails to advance the relevant input generation.
