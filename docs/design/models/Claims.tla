---- MODULE Claims ----
EXTENDS Naturals, FiniteSets
CONSTANTS Workers, MaxFence, BrokenExpiry
VARIABLES holder, fence, live, exposure, oldFence, acceptedFence
vars == <<holder, fence, live, exposure, oldFence, acceptedFence>>
Init == /\ holder = "none" /\ fence = 0 /\ live = FALSE
        /\ exposure = FALSE /\ oldFence = 0 /\ acceptedFence = 0
Claim(w) == /\ holder = "none" /\ ~exposure /\ fence < MaxFence
            /\ holder' = w /\ fence' = fence + 1 /\ live' = TRUE
            /\ UNCHANGED <<exposure, oldFence, acceptedFence>>
Expire == /\ holder # "none" /\ live
          /\ live' = FALSE /\ oldFence' = fence
          /\ holder' = IF BrokenExpiry THEN "none" ELSE holder
          /\ exposure' = IF BrokenExpiry THEN FALSE ELSE exposure
          /\ UNCHANGED <<fence, acceptedFence>>
Renew(w, f) == /\ holder = w /\ live /\ f = fence
              /\ UNCHANGED vars
Write(w, f) == /\ holder = w /\ live /\ f = fence
              /\ exposure' = TRUE /\ acceptedFence' = f
              /\ UNCHANGED <<holder, fence, live, oldFence>>
Reconcile == /\ holder # "none" /\ ~exposure
             /\ holder' = "none" /\ live' = FALSE
             /\ UNCHANGED <<fence, exposure, oldFence, acceptedFence>>
Next == (\E w \in Workers : Claim(w) \/ Renew(w, oldFence) \/ Write(w, oldFence) \/ Write(w, fence))
        \/ Expire \/ Reconcile
ExposureRetained == acceptedFence = 0 \/ exposure
NoStaleAcceptance == acceptedFence = 0 \/ acceptedFence = fence
TypeOK == /\ holder \in Workers \cup {"none"} /\ fence \in 0..MaxFence
          /\ live \in BOOLEAN /\ exposure \in BOOLEAN
Spec == Init /\ [][Next]_vars
====
