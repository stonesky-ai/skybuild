---------------- MODULE AttemptCloseout ----------------
EXTENDS Naturals, Sequences, FiniteSets

CONSTANTS Cutoff, GraceEnd, FinishCurrent, CheckpointStop, Policy, Broken
ASSUME Cutoff \in Nat
ASSUME GraceEnd \in Nat
ASSUME Cutoff < GraceEnd
ASSUME Policy \in {FinishCurrent, CheckpointStop}
ASSUME Broken \in BOOLEAN

PinnedTask == "task-1"
VARIABLES now, clockReady, contact, clockParked, operatorStop, stopAck,
          process, exposureHeld, foreignConflict, admissions, calls, journal

vars == <<now, clockReady, contact, clockParked, operatorStop, stopAck,
          process, exposureHeld, foreignConflict, admissions, calls, journal>>

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
    /\ admissions = <<>>
    /\ calls = <<>>
    /\ journal = <<>>

Advance ==
    /\ now < 4
    /\ now' = now + 1
    /\ UNCHANGED <<clockReady, contact, clockParked, operatorStop, stopAck,
                    process, exposureHeld, foreignConflict, admissions, calls, journal>>

ObserveClock ==
    /\ ~clockReady
    /\ ~clockParked
    /\ clockReady' = TRUE
    /\ journal' = Append(journal, [kind |-> "clock_sample", at |-> now])
    /\ UNCHANGED <<now, contact, clockParked, operatorStop, stopAck,
                    process, exposureHeld, foreignConflict, admissions, calls>>

Restart ==
    /\ clockReady
    /\ clockReady' = FALSE
    /\ UNCHANGED <<now, contact, clockParked, operatorStop, stopAck,
                    process, exposureHeld, foreignConflict, admissions, calls, journal>>

LoseContact ==
    /\ contact = "connected"
    /\ contact' = "lost"
    /\ journal' = Append(journal, [kind |-> "contact_lost", at |-> now])
    /\ UNCHANGED <<now, clockReady, clockParked, operatorStop, stopAck,
                    process, exposureHeld, foreignConflict, admissions, calls>>

RestoreContact ==
    /\ contact = "lost"
    /\ contact' = "connected"
    /\ journal' = Append(journal, [kind |-> "contact_restored", at |-> now])
    /\ UNCHANGED <<now, clockReady, clockParked, operatorStop, stopAck,
                    process, exposureHeld, foreignConflict, admissions, calls>>

ParkClock ==
    /\ ~clockParked
    /\ clockParked' = TRUE
    /\ clockReady' = FALSE
    /\ journal' = Append(journal, [kind |-> "clock_parked", at |-> now])
    /\ UNCHANGED <<now, contact, operatorStop, stopAck, process,
                    exposureHeld, foreignConflict, admissions, calls>>

ReconcileClock ==
    /\ clockParked
    /\ clockParked' = FALSE
    /\ clockReady' = FALSE
    /\ journal' = Append(journal, [kind |-> "clock_reconciled", at |-> now,
                                   cutoff |-> Cutoff])
    /\ UNCHANGED <<now, contact, operatorStop, stopAck, process,
                    exposureHeld, foreignConflict, admissions, calls>>

AdmitPinnedTask ==
    /\ clockReady
    /\ now < Cutoff
    /\ contact = "connected"
    /\ ~clockParked
    /\ ~operatorStop
    /\ ~foreignConflict
    /\ admissions' = Append(admissions, [task |-> PinnedTask, at |-> now])
    /\ clockReady' = FALSE
    /\ journal' = Append(journal, [kind |-> "admit", task |-> PinnedTask, at |-> now])
    /\ UNCHANGED <<now, contact, clockParked, operatorStop, stopAck,
                    process, exposureHeld, foreignConflict, calls>>

TaskCall ==
    /\ clockReady
    /\ now < Cutoff
    /\ ~clockParked
    /\ ~operatorStop
    /\ ~foreignConflict
    /\ process = "running"
    /\ Len(admissions) > 0
    /\ (contact = "connected" \/ Policy = FinishCurrent)
    /\ calls' = Append(calls, [kind |-> "task", at |-> now,
                               parked |-> clockParked, clocked |-> clockReady])
    /\ clockReady' = FALSE
    /\ journal' = Append(journal, [kind |-> "task_call", at |-> now])
    /\ UNCHANGED <<now, contact, clockParked, operatorStop, stopAck,
                    process, exposureHeld, foreignConflict, admissions>>

BrokenLateTaskCall ==
    /\ Broken
    /\ clockReady
    /\ now >= GraceEnd
    /\ ~clockParked
    /\ ~operatorStop
    /\ ~foreignConflict
    /\ process = "running"
    /\ Len(admissions) > 0
    /\ calls' = Append(calls, [kind |-> "task", at |-> now,
                               parked |-> clockParked, clocked |-> clockReady])
    /\ clockReady' = FALSE
    /\ journal' = Append(journal, [kind |-> "broken_task_call", at |-> now])
    /\ UNCHANGED <<now, contact, clockParked, operatorStop, stopAck,
                    process, exposureHeld, foreignConflict, admissions>>

CloseoutCall ==
    /\ clockReady
    /\ Cutoff <= now
    /\ now < GraceEnd
    /\ ~clockParked
    /\ ~operatorStop
    /\ ~foreignConflict
    /\ process = "running"
    /\ calls' = Append(calls, [kind |-> "closeout", at |-> now,
                               parked |-> clockParked, clocked |-> clockReady])
    /\ clockReady' = FALSE
    /\ journal' = Append(journal, [kind |-> "closeout_call", at |-> now])
    /\ UNCHANGED <<now, contact, clockParked, operatorStop, stopAck,
                    process, exposureHeld, foreignConflict, admissions>>

RecordCheckpoint ==
    /\ journal' = Append(journal, [kind |-> "checkpoint", task |-> PinnedTask, at |-> now])
    /\ UNCHANGED <<now, clockReady, contact, clockParked, operatorStop, stopAck,
                    process, exposureHeld, foreignConflict, admissions, calls>>

RequestOperatorStop ==
    /\ ~operatorStop
    /\ operatorStop' = TRUE
    /\ journal' = Append(journal, [kind |-> "operator_stop_requested", at |-> now])
    /\ UNCHANGED <<now, clockReady, contact, clockParked, stopAck, process,
                    exposureHeld, foreignConflict, admissions, calls>>

ObserveUnknown ==
    /\ process # "exited"
    /\ process' = "unknown"
    /\ exposureHeld' = TRUE
    /\ journal' = Append(journal, [kind |-> "process_unknown", at |-> now])
    /\ UNCHANGED <<now, clockReady, contact, clockParked, operatorStop, stopAck,
                    foreignConflict, admissions, calls>>

ObserveExited ==
    /\ process # "exited"
    /\ process' = "exited"
    /\ journal' = Append(journal, [kind |-> "process_exited", at |-> now])
    /\ UNCHANGED <<now, clockReady, contact, clockParked, operatorStop, stopAck,
                    exposureHeld, foreignConflict, admissions, calls>>

ObserveForeignInvocation ==
    /\ foreignConflict = FALSE
    /\ foreignConflict' = TRUE
    /\ exposureHeld' = TRUE
    /\ journal' = Append(journal, [kind |-> "foreign_process_identity", at |-> now])
    /\ UNCHANGED <<now, clockReady, contact, clockParked, operatorStop, stopAck,
                    process, admissions, calls>>

AcknowledgeOperatorStop ==
    /\ operatorStop
    /\ process = "exited"
    /\ stopAck = FALSE
    /\ stopAck' = TRUE
    /\ journal' = Append(journal, [kind |-> "operator_stop_observed", at |-> now])
    /\ UNCHANGED <<now, clockReady, contact, clockParked, operatorStop, process,
                    exposureHeld, foreignConflict, admissions, calls>>

ReconcileExposure ==
    /\ process = "exited"
    /\ ~foreignConflict
    /\ exposureHeld
    /\ exposureHeld' = FALSE
    /\ journal' = Append(journal, [kind |-> "exposure_reconciled", at |-> now])
    /\ UNCHANGED <<now, clockReady, contact, clockParked, operatorStop, stopAck,
                    process, foreignConflict, admissions, calls>>

JournalRoom == Len(journal) < 8
Next == Advance \/ Restart \/ (JournalRoom /\ (ObserveClock \/ LoseContact \/ RestoreContact
        \/ ParkClock \/ ReconcileClock \/ AdmitPinnedTask \/ TaskCall
        \/ BrokenLateTaskCall \/ CloseoutCall \/ RecordCheckpoint
        \/ RequestOperatorStop \/ ObserveUnknown \/ ObserveExited
        \/ ObserveForeignInvocation \/ AcknowledgeOperatorStop \/ ReconcileExposure))

Spec == Init /\ [][Next]_vars

OnlyPinnedTask == \A i \in 1..Len(admissions) : admissions[i].task = PinnedTask
AdmissionsBeforeCutoff == \A i \in 1..Len(admissions) : admissions[i].at < Cutoff
CallsRespectWindow ==
    \A i \in 1..Len(calls) :
        IF calls[i].kind = "task"
        THEN calls[i].at < Cutoff
        ELSE Cutoff <= calls[i].at /\ calls[i].at < GraceEnd
NoCallWhileClockParked == \A i \in 1..Len(calls) : ~calls[i].parked
EveryCallHasCurrentClock == \A i \in 1..Len(calls) : calls[i].clocked
UnknownRetainsExposure == process = "unknown" => exposureHeld
StopAckNeedsOperatorAndExit == stopAck => operatorStop /\ process = "exited"
UncertainOrRunningRetainsExposure == process # "exited" => exposureHeld
ForeignIdentityRetainsExposure == foreignConflict => exposureHeld

===============================================================
