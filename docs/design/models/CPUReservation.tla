---- MODULE CPUReservation ----
EXTENDS Naturals, FiniteSets
CONSTANTS Actions, Capacity, MaxGeneration, BrokenCapacity, BrokenReplay, BrokenFence
VARIABLES state, held, cancelled, exposure, live, central, local,
          generation, localGeneration, taskRevision, snapshot, invalidAdmission
vars == <<state, held, cancelled, exposure, live, central, local,
          generation, localGeneration, taskRevision, snapshot, invalidAdmission>>
Init ==
    /\ state = [a \in Actions |-> "new"]
    /\ held = {} /\ cancelled = {} /\ exposure = {}
    /\ live = Actions /\ central = TRUE /\ local = TRUE
    /\ generation = 1 /\ localGeneration = 1 /\ taskRevision = 1
    /\ snapshot = [a \in Actions |-> <<0, 0, 0>>]
    /\ invalidAdmission = FALSE
Check(a) ==
    /\ state[a] = "new"
    /\ state' = [state EXCEPT ![a] = "checked"]
    /\ snapshot' = [snapshot EXCEPT ![a] = <<generation, localGeneration, taskRevision>>]
    /\ UNCHANGED <<held, cancelled, exposure, live, central, local,
                    generation, localGeneration, taskRevision, invalidAdmission>>
Current(a) == central /\ local /\ a \in live
              /\ snapshot[a] = <<generation, localGeneration, taskRevision>>
Reserve(a) ==
    /\ state[a] = "checked"
    /\ (BrokenFence \/ Current(a))
    /\ (BrokenCapacity \/ Cardinality(held) < Capacity)
    /\ state' = [state EXCEPT ![a] = "reserved"]
    /\ held' = held \cup {a}
    /\ invalidAdmission' = (invalidAdmission \/ ~Current(a))
    /\ UNCHANGED <<cancelled, exposure, live, central, local,
                    generation, localGeneration, taskRevision, snapshot>>
Cancel(a) ==
    /\ state[a] = "reserved" /\ a \notin exposure
    /\ state' = [state EXCEPT ![a] = "cancelled"]
    /\ held' = held \ {a} /\ cancelled' = cancelled \cup {a}
    /\ UNCHANGED <<exposure, live, central, local, generation,
                    localGeneration, taskRevision, snapshot, invalidAdmission>>
Replay(a) ==
    /\ state[a] = "cancelled"
    /\ state' = IF BrokenReplay THEN [state EXCEPT ![a] = "reserved"] ELSE state
    /\ held' = IF BrokenReplay THEN held \cup {a} ELSE held
    /\ UNCHANGED <<cancelled, exposure, live, central, local,
                    generation, localGeneration, taskRevision, snapshot, invalidAdmission>>
Unknown(a) ==
    /\ state[a] = "reserved" /\ a \notin exposure
    /\ exposure' = exposure \cup {a}
    /\ UNCHANGED <<state, held, cancelled, live, central, local,
                    generation, localGeneration, taskRevision, snapshot, invalidAdmission>>
Expire(a) ==
    /\ a \in live /\ live' = live \ {a}
    /\ UNCHANGED <<state, held, cancelled, exposure, central, local,
                    generation, localGeneration, taskRevision, snapshot, invalidAdmission>>
CentralControl ==
    /\ generation < MaxGeneration
    /\ central' = ~central /\ generation' = generation + 1
    /\ UNCHANGED <<state, held, cancelled, exposure, live, local,
                    localGeneration, taskRevision, snapshot, invalidAdmission>>
LocalControl ==
    /\ localGeneration < MaxGeneration
    /\ local' = ~local /\ localGeneration' = localGeneration + 1
    /\ UNCHANGED <<state, held, cancelled, exposure, live, central,
                    generation, taskRevision, snapshot, invalidAdmission>>
Revise ==
    /\ taskRevision < MaxGeneration /\ taskRevision' = taskRevision + 1
    /\ UNCHANGED <<state, held, cancelled, exposure, live, central, local,
                    generation, localGeneration, snapshot, invalidAdmission>>
Next == CentralControl \/ LocalControl \/ Revise \/
        (\E a \in Actions : Check(a) \/ Reserve(a) \/ Cancel(a) \/ Replay(a) \/ Unknown(a) \/ Expire(a))
Spec == Init /\ [][Next]_vars
WithinCapacity == Cardinality(held) <= Capacity
NoRevival == cancelled \cap held = {}
ExposureRetained == exposure \subseteq held
NoStaleAdmission == ~invalidAdmission
TypeOK == /\ state \in [Actions -> {"new", "checked", "reserved", "cancelled"}]
          /\ held \subseteq Actions /\ cancelled \subseteq Actions /\ exposure \subseteq Actions
          /\ live \subseteq Actions /\ central \in BOOLEAN /\ local \in BOOLEAN
          /\ generation \in 1..MaxGeneration /\ localGeneration \in 1..MaxGeneration
          /\ taskRevision \in 1..MaxGeneration /\ invalidAdmission \in BOOLEAN
====
