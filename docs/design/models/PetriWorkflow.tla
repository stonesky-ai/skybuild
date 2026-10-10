------------------------- MODULE PetriWorkflow -------------------------
EXTENDS Naturals, FiniteSets
CONSTANTS Workers, MaxFence, BrokenFence, BrokenLatePass
VARIABLES marking, owner, fence, captured, writes, failed, pending, effect
vars == <<marking, owner, fence, captured, writes, failed, pending, effect>>
Places == {"Ready", "Working", "Validating", "Integrating", "Done", "Deferred", "Hold"}
At(place) == marking = {place}
Move(place) == marking' = {place}
Init == /\ marking = {"Ready"}
        /\ owner = "none"
        /\ fence = 0
        /\ captured = [w \in Workers |-> 0]
        /\ writes = {}
        /\ failed = FALSE
        /\ pending = "none"
        /\ effect = "resolved"
Claim(w) == /\ At("Ready") /\ owner = "none"
            /\ effect = "resolved" /\ pending = "none" /\ fence < MaxFence
            /\ Move("Working") /\ owner' = w /\ fence' = fence + 1
            /\ captured' = [captured EXCEPT ![w] = fence + 1]
            /\ failed' = FALSE
            /\ UNCHANGED <<writes, pending, effect>>
ReconcileClaim == /\ owner # "none" /\ At("Working") /\ effect = "resolved"
          /\ Move("Ready") /\ owner' = "none"
          /\ UNCHANGED <<fence, captured, writes, failed, pending, effect>>
Submit(w) == /\ At("Working") /\ captured[w] > 0 /\ pending = "none"
             /\ (BrokenFence \/ (owner = w /\ captured[w] = fence))
             /\ Move("Validating")
             /\ writes' = writes \cup {[expected |-> captured[w], actual |-> fence, worker |-> w, holder |-> owner]}
             /\ UNCHANGED <<owner, fence, captured, failed, pending, effect>>
Pass == /\ (At("Validating") \/ (BrokenLatePass /\ At("Ready") /\ failed))
        /\ Move("Validating")
        /\ UNCHANGED <<owner, fence, captured, writes, failed, pending, effect>>
Fail == /\ At("Validating") /\ Move("Ready") /\ failed' = TRUE
        /\ UNCHANGED <<owner, fence, captured, writes, pending, effect>>
Freeze == /\ At("Validating") /\ ~failed /\ pending = "none"
          /\ effect = "resolved" /\ Move("Integrating")
          /\ UNCHANGED <<owner, fence, captured, writes, failed, pending, effect>>
Dispatch == /\ At("Integrating") /\ effect = "resolved" /\ pending = "none"
            /\ effect' = "unknown"
            /\ UNCHANGED <<marking, owner, fence, captured, writes, failed, pending>>
Control(kind) == /\ marking \subseteq {"Ready", "Working", "Validating", "Integrating"}
                 /\ pending = "none" /\ pending' = kind
                 /\ UNCHANGED <<marking, owner, fence, captured, writes, failed, effect>>
Resolve == /\ effect = "unknown" /\ effect' = "resolved"
           /\ UNCHANGED <<marking, owner, fence, captured, writes, failed, pending>>
QuiesceClaim == /\ owner # "none" /\ effect = "resolved"
                /\ (pending # "none" \/ At("Ready") \/ At("Validating") \/ At("Integrating"))
                /\ owner' = "none"
                /\ UNCHANGED <<marking, fence, captured, writes, failed, pending, effect>>
ApplyControl == /\ owner = "none" /\ pending \in {"Hold", "Deferred"} /\ effect = "resolved"
                /\ Move(pending) /\ pending' = "none" /\ owner' = "none"
                /\ UNCHANGED <<fence, captured, writes, failed, effect>>
Release == /\ marking \subseteq {"Hold", "Deferred"} /\ Move("Ready")
           /\ UNCHANGED <<owner, fence, captured, writes, failed, pending, effect>>
Exclude == /\ At("Integrating") /\ effect = "resolved" /\ pending = "none"
           /\ Move("Validating")
           /\ UNCHANGED <<owner, fence, captured, writes, failed, pending, effect>>
Observe == /\ At("Integrating") /\ UNCHANGED vars
Accept == /\ At("Integrating") /\ effect = "resolved" /\ pending = "none"
          /\ Move("Done") /\ owner' = "none"
          /\ UNCHANGED <<fence, captured, writes, failed, pending, effect>>
Next == ReconcileClaim \/ Pass \/ Fail \/ Freeze \/ Dispatch \/ Resolve \/ ApplyControl \/ QuiesceClaim \/ Release \/ Accept \/ Exclude \/ Observe
        \/ (\E w \in Workers : Claim(w) \/ Submit(w))
        \/ (\E kind \in {"Hold", "Deferred"} : Control(kind))
Spec == Init /\ [][Next]_vars
TypeOK == /\ marking \subseteq Places /\ owner \in Workers \cup {"none"}
          /\ fence \in 0..MaxFence /\ captured \in [Workers -> 0..MaxFence]
          /\ failed \in BOOLEAN /\ pending \in {"none", "Hold", "Deferred"}
          /\ effect \in {"resolved", "unknown"}
TokenConservation == Cardinality(marking) = 1
CurrentFenceWrites == \A write \in writes : write.expected = write.actual /\ write.worker = write.holder
FailureCannotAdvance == failed => marking \cap {"Validating", "Integrating", "Done"} = {}
UnknownPublicationRetained == effect = "unknown" => At("Integrating")
=============================================================================
