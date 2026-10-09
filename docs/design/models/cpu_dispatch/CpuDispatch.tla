---- MODULE CpuDispatch ----
EXTENDS Naturals, FiniteSets
CONSTANTS Ops, BrokenRelease
VARIABLES generation, enabled, fence, revision, ready, held, phase,
          bound, adapter, starts, observed, stopped
vars == <<generation, enabled, fence, revision, ready, held, phase,
          bound, adapter, starts, observed, stopped>>
Snapshot == <<generation, fence, revision>>
Init == /\ generation = 1 /\ enabled = TRUE
        /\ fence = 1 /\ revision = 1 /\ ready = TRUE
        /\ held = {} /\ stopped = {}
        /\ phase = [o \in Ops |-> "absent"]
        /\ bound = [o \in Ops |-> <<0, 0, 0>>]
        /\ adapter = [o \in Ops |-> "absent"]
        /\ starts = [o \in Ops |-> 0]
        /\ observed = {}
Reserve(o) == /\ phase[o] = "absent" /\ held = {}
              /\ enabled /\ ready
              /\ phase' = [phase EXCEPT ![o] = "reserved"]
              /\ held' = {o}
              /\ bound' = [bound EXCEPT ![o] = Snapshot]
              /\ UNCHANGED <<generation, enabled, fence, revision, ready,
                              adapter, starts, observed, stopped>>
Prepare(o) == /\ phase[o] = "reserved" /\ bound[o] = Snapshot
              /\ enabled /\ ready
              /\ phase' = [phase EXCEPT ![o] = "unknown"]
              /\ UNCHANGED <<generation, enabled, fence, revision, ready, held,
                              bound, adapter, starts, observed, stopped>>
Start(o) == /\ phase[o] = "unknown" /\ adapter[o] = "absent"
            /\ o \notin stopped /\ bound[o] = Snapshot
            /\ enabled /\ ready
            /\ adapter' = [adapter EXCEPT ![o] = "running"]
            /\ starts' = [starts EXCEPT ![o] = @ + 1]
            /\ UNCHANGED <<generation, enabled, fence, revision, ready, held,
                            phase, bound, observed, stopped>>
StopRequest(o) == /\ phase[o] = "unknown" /\ o \notin stopped
                  /\ stopped' = stopped \cup {o}
                  /\ UNCHANGED <<generation, enabled, fence, revision, ready, held,
                                  phase, bound, adapter, starts, observed>>
StopAck(o) == /\ phase[o] = "unknown" /\ o \in stopped
             /\ adapter[o] \in {"absent", "running"}
             /\ adapter' = [adapter EXCEPT ![o] = "terminal"]
             /\ UNCHANGED <<generation, enabled, fence, revision, ready, held,
                             phase, bound, starts, observed, stopped>>
Observe(o) == /\ phase[o] = "unknown" /\ o \notin observed
              /\ observed' = observed \cup {o}
              /\ UNCHANGED <<generation, enabled, fence, revision, ready, held,
                              phase, bound, adapter, starts, stopped>>
Reconcile(o) == /\ phase[o] = "unknown"
                /\ (adapter[o] = "terminal" \/ (BrokenRelease /\ o \in observed))
                /\ phase' = [phase EXCEPT ![o] = "settled"]
                /\ held' = held \ {o}
                /\ UNCHANGED <<generation, enabled, fence, revision, ready,
                                bound, adapter, starts, observed, stopped>>
ChangeControls == /\ generation < 3
                  /\ generation' = generation + 1 /\ enabled' = ~enabled
                  /\ UNCHANGED <<fence, revision, ready, held, phase, bound,
                                  adapter, starts, observed, stopped>>
Invalidate == /\ ready /\ revision = 1
              /\ revision' = 2 /\ ready' = FALSE
              /\ UNCHANGED <<generation, enabled, fence, held, phase, bound,
                              adapter, starts, observed, stopped>>
ExpireClaim == /\ fence = 1 /\ fence' = 2
               /\ UNCHANGED <<generation, enabled, revision, ready, held, phase,
                               bound, adapter, starts, observed, stopped>>
Next == (\E o \in Ops : Reserve(o) \/ Prepare(o) \/ Start(o) \/ StopRequest(o)
                        \/ StopAck(o) \/ Observe(o) \/ Reconcile(o))
        \/ ChangeControls \/ Invalidate \/ ExpireClaim
Spec == Init /\ [][Next]_vars
Capacity == Cardinality(held) <= 1
UnknownHeld == \A o \in Ops : phase[o] = "unknown" => o \in held
TerminalProof == \A o \in Ops : phase[o] = "settled" => adapter[o] = "terminal"
SingleStart == \A o \in Ops : starts[o] <= 1
NoOrphan == \A o \in Ops : adapter[o] = "running" => o \in held
PreparedBeforeStart == \A o \in Ops : starts[o] > 0 => phase[o] \in {"unknown", "settled"}
====
