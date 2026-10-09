---- MODULE ObservationJournal ----
EXTENDS Naturals, FiniteSets
CONSTANTS Identities, Sequences
VARIABLES journal, pinned, latest, online, authority
vars == <<journal, pinned, latest, online, authority>>
None == "none"
Init == /\ journal = {}
        /\ pinned = None
        /\ latest = 0
        /\ online = TRUE
        /\ authority = "held"
Receive(i, s) ==
    /\ online
    /\ journal' = journal \cup {<<i, s>>}
    /\ pinned' = IF pinned = None THEN i ELSE pinned
    /\ latest' = IF (pinned = None \/ pinned = i) /\ s > latest THEN s ELSE latest
    /\ UNCHANGED <<online, authority>>
Crash == /\ online' = ~online
         /\ UNCHANGED <<journal, pinned, latest, authority>>
Next == (\E i \in Identities, s \in Sequences : Receive(i, s)) \/ Crash
AuthorityHeld == authority = "held"
ProjectionFromJournal == latest = 0 \/ <<pinned, latest>> \in journal
ProjectionMaximum == pinned = None \/ latest = CHOOSE s \in Sequences :
    /\ <<pinned, s>> \in journal
    /\ \A t \in Sequences : <<pinned, t>> \in journal => t <= s
Spec == Init /\ [][Next]_vars
====
