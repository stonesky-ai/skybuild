--------------------------- MODULE Admission ---------------------------
EXTENDS Naturals, FiniteSets

CONSTANTS Workers, Actions, Capacity, MaxGeneration, BrokenReserve, BrokenFence
NoWorker == "no-worker"
NoAction == "no-action"
VARIABLES phase, action, permitGeneration, sawCapacity, owner, held,
          starts, enabled, localEnabled, generation, invalidRedemption
vars == <<phase, action, permitGeneration, sawCapacity, owner, held,
          starts, enabled, localEnabled, generation, invalidRedemption>>

Init ==
    /\ phase = [w \in Workers |-> "idle"]
    /\ action = [w \in Workers |-> NoAction]
    /\ permitGeneration = [w \in Workers |-> 0]
    /\ sawCapacity = [w \in Workers |-> FALSE]
    /\ owner = [a \in Actions |-> NoWorker]
    /\ held = {}
    /\ starts = [a \in Actions |-> 0]
    /\ enabled = TRUE
    /\ localEnabled = [w \in Workers |-> TRUE]
    /\ generation = 0
    /\ invalidRedemption = FALSE

Check(w, a) ==
    /\ phase[w] = "idle"
    /\ phase' = [phase EXCEPT ![w] = "checked"]
    /\ action' = [action EXCEPT ![w] = a]
    /\ permitGeneration' = [permitGeneration EXCEPT ![w] = generation]
    /\ sawCapacity' = [sawCapacity EXCEPT ![w] = Cardinality(held) < Capacity]
    /\ UNCHANGED <<owner, held, starts, enabled, localEnabled, generation,
                    invalidRedemption>>

Reserve(w) ==
    /\ phase[w] = "checked"
    /\ enabled /\ localEnabled[w]
    /\ permitGeneration[w] = generation
    /\ owner[action[w]] = NoWorker
    /\ (IF BrokenReserve THEN sawCapacity[w] ELSE Cardinality(held) < Capacity)
    /\ phase' = [phase EXCEPT ![w] = "reserved"]
    /\ owner' = [owner EXCEPT ![action[w]] = w]
    /\ held' = held \cup {w}
    /\ UNCHANGED <<action, permitGeneration, sawCapacity, starts, enabled,
                    localEnabled, generation, invalidRedemption>>

Redeem(w) ==
    /\ phase[w] = "reserved"
    /\ (IF BrokenFence THEN TRUE
        ELSE enabled /\ localEnabled[w] /\ permitGeneration[w] = generation)
    /\ phase' = [phase EXCEPT ![w] = "redeemed"]
    /\ starts' = [starts EXCEPT ![action[w]] = @ + 1]
    /\ invalidRedemption' = (invalidRedemption \/ ~enabled \/ ~localEnabled[w]
                              \/ permitGeneration[w] # generation)
    /\ UNCHANGED <<action, permitGeneration, sawCapacity, owner, held,
                    enabled, localEnabled, generation>>

Terminal(w) ==
    /\ phase[w] = "redeemed"
    /\ phase' = [phase EXCEPT ![w] = "done"]
    /\ held' = held \ {w}
    /\ UNCHANGED <<action, permitGeneration, sawCapacity, owner, starts,
                    enabled, localEnabled, generation, invalidRedemption>>

Control ==
    /\ generation < MaxGeneration
    /\ enabled' = ~enabled
    /\ generation' = generation + 1
    /\ UNCHANGED <<phase, action, permitGeneration, sawCapacity, owner, held,
                    starts, localEnabled, invalidRedemption>>

LocalControl(w) ==
    /\ localEnabled' = [localEnabled EXCEPT ![w] = ~@]
    /\ UNCHANGED <<phase, action, permitGeneration, sawCapacity, owner, held,
                    starts, enabled, generation, invalidRedemption>>

Next == Control \/ (\E w \in Workers :
    LocalControl(w) \/ Reserve(w) \/ Redeem(w) \/ Terminal(w)
    \/ (\E a \in Actions : Check(w, a)))
Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ phase \in [Workers -> {"idle", "checked", "reserved", "redeemed", "done"}]
    /\ action \in [Workers -> Actions \cup {NoAction}]
    /\ permitGeneration \in [Workers -> 0..MaxGeneration]
    /\ sawCapacity \in [Workers -> BOOLEAN]
    /\ owner \in [Actions -> Workers \cup {NoWorker}]
    /\ held \subseteq Workers
    /\ starts \in [Actions -> 0..Cardinality(Workers)]
    /\ enabled \in BOOLEAN /\ localEnabled \in [Workers -> BOOLEAN]
    /\ generation \in 0..MaxGeneration
    /\ invalidRedemption \in BOOLEAN
WithinCapacity == Cardinality(held) <= Capacity
AtMostOnce == \A a \in Actions : starts[a] <= 1
Ownership == \A w \in held : owner[action[w]] = w
NoInvalidRedemption == ~invalidRedemption
ReservationCoversRun == \A w \in Workers : phase[w] = "redeemed" => w \in held
=============================================================================
