---- MODULE BundlePreparation ----
EXTENDS Naturals, FiniteSets
CONSTANTS Preparers, Integrators, Limit, Baseline, BrokenCapacity, BrokenPublication
Actors == Preparers \cup Integrators
VARIABLES trees, publishers, phase, observed
vars == <<trees, publishers, phase, observed>>
Need(a) == IF a \in Preparers THEN 2 ELSE 1
Count == Baseline + Cardinality(trees)
Init == /\ trees = {}
        /\ publishers = {}
        /\ phase = [a \in Actors |-> "idle"]
        /\ observed = [a \in Actors |-> 0]
Acquire(a) == /\ a \in Integrators /\ phase[a] = "idle"
              /\ (BrokenPublication \/ publishers = {})
              /\ publishers' = publishers \cup {a}
              /\ phase' = [phase EXCEPT ![a] = "waiting"]
              /\ UNCHANGED <<trees, observed>>
Eligible(a) == (a \in Preparers /\ phase[a] = "idle") \/
               (a \in Integrators /\ phase[a] = "waiting")
Create(a) == /\ ~BrokenCapacity /\ Eligible(a) /\ Count + Need(a) <= Limit
             /\ trees' = trees \cup {a}
             /\ phase' = [phase EXCEPT ![a] = IF a \in Integrators THEN "gating" ELSE "prepared"]
             /\ UNCHANGED <<publishers, observed>>
UnsafeCheck(a) == /\ BrokenCapacity /\ Eligible(a)
                  /\ observed' = [observed EXCEPT ![a] = Count]
                  /\ phase' = [phase EXCEPT ![a] = "checked"]
                  /\ UNCHANGED <<trees, publishers>>
UnsafeCreate(a) == /\ BrokenCapacity /\ phase[a] = "checked"
                   /\ observed[a] + Need(a) <= Limit
                   /\ trees' = trees \cup {a}
                   /\ phase' = [phase EXCEPT ![a] = IF a \in Integrators THEN "gating" ELSE "prepared"]
                   /\ UNCHANGED <<publishers, observed>>
Finish(a) == /\ phase[a] \in {"gating", "prepared"}
             /\ trees' = trees \ {a}
             /\ publishers' = publishers \ {a}
             /\ phase' = [phase EXCEPT ![a] = "done"]
             /\ UNCHANGED observed
Crash(a) == /\ phase[a] \in {"waiting", "checked", "gating", "prepared"}
            /\ publishers' = publishers \ {a}
            /\ phase' = [phase EXCEPT ![a] = "crashed"]
            /\ UNCHANGED <<trees, observed>>
Next == \E a \in Actors : Acquire(a) \/ Create(a) \/ UnsafeCheck(a) \/ UnsafeCreate(a) \/ Finish(a) \/ Crash(a)
Capacity == Count <= Limit
OnePublisher == Cardinality(publishers) <= 1
NoOverlap == ~(\E p \in Preparers, i \in Integrators : phase[p] = "prepared" /\ phase[i] = "gating")
Spec == Init /\ [][Next]_vars
====
