---- MODULE Qualification ----
EXTENDS Naturals, FiniteSets
CONSTANT BrokenRestart
VARIABLES phase, sent, reserved, qualified, approved, crashed, blocked
vars == <<phase, sent, reserved, qualified, approved, crashed, blocked>>
Ops == 1..2
Init == /\ phase = [i \in Ops |-> "idle"]
        /\ sent = [i \in Ops |-> 0]
        /\ reserved = 0
        /\ qualified = FALSE /\ approved = TRUE
        /\ crashed = FALSE /\ blocked = FALSE
Qualify == /\ ~crashed /\ ~blocked /\ approved /\ ~qualified
           /\ qualified' = TRUE
           /\ UNCHANGED <<phase, sent, reserved, approved, crashed, blocked>>
Intent(i) == /\ ~crashed /\ ~blocked /\ approved /\ qualified
             /\ phase[i] = "idle"
             /\ \A j \in Ops : phase[j] \notin {"pending", "inflight"}
             /\ reserved + 3 <= 6
             /\ phase' = [phase EXCEPT ![i] = "pending"]
             /\ reserved' = reserved + 3
             /\ UNCHANGED <<sent, qualified, approved, crashed, blocked>>
Send(i) == /\ ~crashed /\ ~blocked /\ approved /\ qualified
           /\ phase[i] = "pending"
           /\ sent' = [sent EXCEPT ![i] = @ + 1]
           /\ phase' = [phase EXCEPT ![i] = "inflight"]
           /\ UNCHANGED <<reserved, qualified, approved, crashed, blocked>>
Reply(i) == /\ ~crashed /\ phase[i] = "inflight"
            /\ phase' = [phase EXCEPT ![i] = "complete"]
            /\ UNCHANGED <<sent, reserved, qualified, approved, crashed, blocked>>
Crash == /\ ~crashed /\ crashed' = TRUE
         /\ phase' = [i \in Ops |-> IF phase[i] = "inflight" THEN "pending" ELSE phase[i]]
         /\ UNCHANGED <<sent, reserved, qualified, approved, blocked>>
Restart == /\ crashed /\ crashed' = FALSE
           /\ blocked' = (blocked \/ (~BrokenRestart /\ \E i \in Ops : phase[i] = "pending"))
           /\ phase' = IF BrokenRestart THEN [i \in Ops |-> IF phase[i] = "pending" THEN "idle" ELSE phase[i]] ELSE phase
           /\ reserved' = IF BrokenRestart THEN 0 ELSE reserved
           /\ UNCHANGED <<sent, qualified, approved>>
Expire == /\ approved /\ approved' = FALSE
          /\ UNCHANGED <<phase, sent, reserved, qualified, crashed, blocked>>
Next == Qualify \/ Crash \/ Restart \/ Expire \/ \E i \in Ops : Intent(i) \/ Send(i) \/ Reply(i)
Budget == reserved <= 6
ExposureRetained == \A i \in Ops : sent[i] > 0 => phase[i] # "idle"
AtMostOnce == \A i \in Ops : sent[i] <= 1
QualifiedEffects == (\E i \in Ops : sent[i] > 0) => qualified
Serial == Cardinality({i \in Ops : phase[i] \in {"pending", "inflight"}}) <= 1
Spec == Init /\ [][Next]_vars
====
