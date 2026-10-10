---------------- MODULE AttemptCloseout ----------------
EXTENDS Naturals, FiniteSets

CONSTANTS Cutoff, GraceEnd, FinishCurrent, CheckpointStop, Policy, Broken
ASSUME Cutoff \in Nat
ASSUME GraceEnd \in Nat
ASSUME Cutoff < GraceEnd
ASSUME Policy \in {FinishCurrent, CheckpointStop}
ASSUME Broken \in BOOLEAN

PinnedTask == "task-1"
VARIABLES now, clockReady, contact, clockParked, operatorStop, stopAck,
          process, exposureHeld, foreignConflict, admitted, admissionAt,
          callsObserved, badCallWindow, badCallParked, badCallClock, journalCount

vars == <<now, clockReady, contact, clockParked, operatorStop, stopAck,
          process, exposureHeld, foreignConflict, admitted, admissionAt,
          callsObserved, badCallWindow, badCallParked, badCallClock, journalCount>>

Init ==
    /\ now = 0
    /\ clockReady = FALSE
    /\ contact = "connected"
    /\ clockParked = FALSE
    /\ operatorStop = FALSE
    /\ stopAck = FALSE
    /\ process = "running"
    /\ exposureHeld = TRUE
    /\ foreignConflict = FALSE
    /\ admitted = FALSE
    /\ admissionAt = 0
    /\ callsObserved = FALSE
    /\ badCallWindow = FALSE
    /\ badCallParked = FALSE
    /\ badCallClock = FALSE
    /\ journalCount = 0

Advance ==
    /\ now < 4
    /\ now' = now + 1
    /\ UNCHANGED <<clockReady, contact, clockParked, operatorStop, stopAck,
                    process, exposureHeld, foreignConflict, admitted, admissionAt,
                    callsObserved, badCallWindow, badCallParked, badCallClock, journalCount>>

ObserveClock ==
    /\ ~clockReady
    /\ ~clockParked
    /\ clockReady' = TRUE
    /\ journalCount' = journalCount + 1
    /\ UNCHANGED <<now, contact, clockParked, operatorStop, stopAck, process,
                    exposureHeld, foreignConflict, admitted, admissionAt,
                    callsObserved, badCallWindow, badCallParked, badCallClock>>

Restart ==
    /\ clockReady
    /\ clockReady' = FALSE
    /\ UNCHANGED <<now, contact, clockParked, operatorStop, stopAck, process,
                    exposureHeld, foreignConflict, admitted, admissionAt,
                    callsObserved, badCallWindow, badCallParked, badCallClock, journalCount>>

LoseContact ==
    /\ contact = "connected"
    /\ contact' = "lost"
    /\ journalCount' = journalCount + 1
    /\ UNCHANGED <<now, clockReady, clockParked, operatorStop, stopAck, process,
                    exposureHeld, foreignConflict, admitted, admissionAt,
                    callsObserved, badCallWindow, badCallParked, badCallClock>>

RestoreContact ==
    /\ contact = "lost"
    /\ contact' = "connected"
    /\ journalCount' = journalCount + 1
    /\ UNCHANGED <<now, clockReady, clockParked, operatorStop, stopAck, process,
                    exposureHeld, foreignConflict, admitted, admissionAt,
                    callsObserved, badCallWindow, badCallParked, badCallClock>>

ParkClock ==
    /\ ~clockParked
    /\ clockParked' = TRUE
    /\ clockReady' = FALSE
    /\ journalCount' = journalCount + 1
    /\ UNCHANGED <<now, contact, operatorStop, stopAck, process, exposureHeld,
                    foreignConflict, admitted, admissionAt, callsObserved,
                    badCallWindow, badCallParked, badCallClock>>

ReconcileClock ==
    /\ clockParked
    /\ clockParked' = FALSE
    /\ clockReady' = FALSE
    /\ journalCount' = journalCount + 1
    /\ UNCHANGED <<now, contact, operatorStop, stopAck, process, exposureHeld,
                    foreignConflict, admitted, admissionAt, callsObserved,
                    badCallWindow, badCallParked, badCallClock>>

AdmitPinnedTask ==
    /\ clockReady
    /\ now < Cutoff
    /\ contact = "connected"
    /\ ~clockParked
    /\ ~operatorStop
    /\ ~foreignConflict
    /\ ~admitted
    /\ admitted' = TRUE
    /\ admissionAt' = now
    /\ clockReady' = FALSE
    /\ journalCount' = journalCount + 1
    /\ UNCHANGED <<now, contact, clockParked, operatorStop, stopAck, process,
                    exposureHeld, foreignConflict, callsObserved, badCallWindow,
                    badCallParked, badCallClock>>

TaskCall ==
    /\ clockReady
    /\ now < Cutoff
    /\ ~clockParked
    /\ ~operatorStop
    /\ ~foreignConflict
    /\ process = "running"
    /\ admitted
    /\ (contact = "connected" \/ Policy = FinishCurrent)
    /\ callsObserved' = TRUE
    /\ badCallWindow' = badCallWindow \/ (now >= Cutoff)
    /\ badCallParked' = badCallParked \/ clockParked
    /\ badCallClock' = badCallClock \/ ~clockReady
    /\ clockReady' = FALSE
    /\ journalCount' = journalCount + 1
    /\ UNCHANGED <<now, contact, clockParked, operatorStop, stopAck, process,
                    exposureHeld, foreignConflict, admitted, admissionAt>>

BrokenLateTaskCall ==
    /\ Broken
    /\ clockReady
    /\ now >= GraceEnd
    /\ ~clockParked
    /\ ~operatorStop
    /\ ~foreignConflict
    /\ process = "running"
    /\ admitted
    /\ callsObserved' = TRUE
    /\ badCallWindow' = badCallWindow \/ (now >= Cutoff)
    /\ badCallParked' = badCallParked \/ clockParked
    /\ badCallClock' = badCallClock \/ ~clockReady
    /\ clockReady' = FALSE
    /\ journalCount' = journalCount + 1
    /\ UNCHANGED <<now, contact, clockParked, operatorStop, stopAck, process,
                    exposureHeld, foreignConflict, admitted, admissionAt>>

CloseoutCall ==
    /\ clockReady
    /\ Cutoff <= now
    /\ now < GraceEnd
    /\ ~clockParked
    /\ ~operatorStop
    /\ ~foreignConflict
    /\ process = "running"
    /\ callsObserved' = TRUE
    /\ badCallWindow' = badCallWindow \/ (now < Cutoff \/ now >= GraceEnd)
    /\ badCallParked' = badCallParked \/ clockParked
    /\ badCallClock' = badCallClock \/ ~clockReady
    /\ clockReady' = FALSE
    /\ journalCount' = journalCount + 1
    /\ UNCHANGED <<now, contact, clockParked, operatorStop, stopAck, process,
                    exposureHeld, foreignConflict, admitted, admissionAt>>

RecordCheckpoint ==
    /\ journalCount' = journalCount + 1
    /\ UNCHANGED <<now, clockReady, contact, clockParked, operatorStop, stopAck,
                    process, exposureHeld, foreignConflict, admitted, admissionAt,
                    callsObserved, badCallWindow, badCallParked, badCallClock>>

RequestOperatorStop ==
    /\ ~operatorStop
    /\ operatorStop' = TRUE
    /\ journalCount' = journalCount + 1
    /\ UNCHANGED <<now, clockReady, contact, clockParked, stopAck, process,
                    exposureHeld, foreignConflict, admitted, admissionAt,
                    callsObserved, badCallWindow, badCallParked, badCallClock>>

ObserveUnknown ==
    /\ process # "exited"
    /\ process' = "unknown"
    /\ exposureHeld' = TRUE
    /\ journalCount' = journalCount + 1
    /\ UNCHANGED <<now, clockReady, contact, clockParked, operatorStop, stopAck,
                    foreignConflict, admitted, admissionAt, callsObserved,
                    badCallWindow, badCallParked, badCallClock>>

ObserveExited ==
    /\ process # "exited"
    /\ process' = "exited"
    /\ journalCount' = journalCount + 1
    /\ UNCHANGED <<now, clockReady, contact, clockParked, operatorStop, stopAck,
                    exposureHeld, foreignConflict, admitted, admissionAt,
                    callsObserved, badCallWindow, badCallParked, badCallClock>>

ObserveForeignInvocation ==
    /\ foreignConflict = FALSE
    /\ foreignConflict' = TRUE
    /\ exposureHeld' = TRUE
    /\ journalCount' = journalCount + 1
    /\ UNCHANGED <<now, clockReady, contact, clockParked, operatorStop, stopAck,
                    process, admitted, admissionAt, callsObserved, badCallWindow,
                    badCallParked, badCallClock>>

AcknowledgeOperatorStop ==
    /\ operatorStop
    /\ process = "exited"
    /\ stopAck = FALSE
    /\ stopAck' = TRUE
    /\ journalCount' = journalCount + 1
    /\ UNCHANGED <<now, clockReady, contact, clockParked, operatorStop, process,
                    exposureHeld, foreignConflict, admitted, admissionAt,
                    callsObserved, badCallWindow, badCallParked, badCallClock>>

ReconcileExposure ==
    /\ process = "exited"
    /\ ~foreignConflict
    /\ exposureHeld
    /\ exposureHeld' = FALSE
    /\ journalCount' = journalCount + 1
    /\ UNCHANGED <<now, clockReady, contact, clockParked, operatorStop, stopAck,
                    process, foreignConflict, admitted, admissionAt, callsObserved,
                    badCallWindow, badCallParked, badCallClock>>

JournalRoom == journalCount < 8
Next == Advance \/ Restart \/ (JournalRoom /\ (ObserveClock \/ LoseContact \/ RestoreContact
        \/ ParkClock \/ ReconcileClock \/ AdmitPinnedTask \/ TaskCall
        \/ BrokenLateTaskCall \/ CloseoutCall \/ RecordCheckpoint
        \/ RequestOperatorStop \/ ObserveUnknown \/ ObserveExited
        \/ ObserveForeignInvocation \/ AcknowledgeOperatorStop \/ ReconcileExposure))

Spec == Init /\ [][Next]_vars

OnlyPinnedTask == PinnedTask = "task-1"
AdmissionsBeforeCutoff == admitted => admissionAt < Cutoff
CallsRespectWindow == ~badCallWindow
NoCallWhileClockParked == ~badCallParked
EveryCallHasCurrentClock == ~badCallClock
CallViolationsHaveObservation == callsObserved \/ (~badCallWindow /\ ~badCallParked /\ ~badCallClock)
UnknownRetainsExposure == process = "unknown" => exposureHeld
StopAckNeedsOperatorAndExit == stopAck => operatorStop /\ process = "exited"
UncertainOrRunningRetainsExposure == process # "exited" => exposureHeld
ForeignIdentityRetainsExposure == foreignConflict => exposureHeld

===============================================================
