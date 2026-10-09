------------------------- MODULE Reassessment -------------------------
EXTENDS Integers
CONSTANTS MaxRevision, BrokenAcknowledge
VARIABLES revision, assessed, captured, projectionInputs, running
vars == <<revision, assessed, captured, projectionInputs, running>>

Init ==
    /\ revision = 0
    /\ assessed = -1
    /\ captured = -1
    /\ projectionInputs = -1
    /\ running = FALSE

Change ==
    /\ revision < MaxRevision
    /\ revision' = revision + 1
    /\ UNCHANGED <<assessed, captured, projectionInputs, running>>

Begin ==
    /\ ~running
    /\ assessed < revision
    /\ captured' = revision
    /\ running' = TRUE
    /\ UNCHANGED <<revision, assessed, projectionInputs>>

Finish ==
    /\ running
    /\ running' = FALSE
    /\ IF BrokenAcknowledge
          THEN /\ assessed' = revision
               /\ projectionInputs' = captured
          ELSE IF captured = revision
                  THEN /\ assessed' = captured
                       /\ projectionInputs' = captured
                  ELSE UNCHANGED <<assessed, projectionInputs>>
    /\ UNCHANGED <<revision, captured>>

Idle == UNCHANGED vars
Next == Change \/ Begin \/ Finish \/ Idle
Spec == Init /\ [][Next]_vars
TypeOK ==
    /\ revision \in 0..MaxRevision
    /\ assessed \in -1..MaxRevision
    /\ captured \in -1..MaxRevision
    /\ projectionInputs \in -1..MaxRevision
    /\ running \in BOOLEAN
NoFalseFresh == assessed = revision => projectionInputs = revision
NoFutureAssessment == assessed <= revision
=======================================================================
