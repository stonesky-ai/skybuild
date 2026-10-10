---- MODULE CPUWorkerRelay ----
EXTENDS Naturals

CONSTANT MaxStarts
VARIABLES usage, lease, controls, reservation, effect, dispatch, unit,
          resultIntent, remoteHead, terminalProof, observation,
          submitIntent, submitOutcome, taskPlace, starts, submitCalls

vars == <<usage, lease, controls, reservation, effect, dispatch, unit,
         resultIntent, remoteHead, terminalProof, observation,
         submitIntent, submitOutcome, taskPlace, starts, submitCalls>>

Init == /\ usage = "clear"
        /\ lease = "live"
        /\ controls = "enabled"
        /\ reservation = "absent"
        /\ effect = "absent"
        /\ dispatch = "absent"
        /\ unit = "absent"
        /\ resultIntent = FALSE
        /\ remoteHead = FALSE
        /\ terminalProof = FALSE
        /\ observation = FALSE
        /\ submitIntent = FALSE
        /\ submitOutcome = "none"
        /\ taskPlace = "working"
        /\ starts = 0
        /\ submitCalls = 0

Reserve == /\ reservation = "absent"
          /\ taskPlace = "working"
          /\ usage = "clear"
          /\ lease = "live"
          /\ reservation' = "reserved"
          /\ UNCHANGED <<usage, lease, controls, effect, dispatch, unit,
                          resultIntent, remoteHead, terminalProof, observation,
                          submitIntent, submitOutcome, taskPlace, starts, submitCalls>>

Prepare == /\ reservation = "reserved"
          /\ effect = "absent"
          /\ dispatch = "absent"
          /\ effect' = "held"
          /\ dispatch' = "prepared"
          /\ UNCHANGED <<usage, lease, controls, reservation, unit,
                          resultIntent, remoteHead, terminalProof, observation,
                          submitIntent, submitOutcome, taskPlace, starts, submitCalls>>

Begin == /\ dispatch = "prepared"
         /\ reservation = "reserved"
         /\ effect = "held"
         /\ usage = "clear"
         /\ lease = "live"
         /\ controls = "enabled"
         /\ starts < MaxStarts
         /\ dispatch' = "launch-intent"
         /\ starts' = starts + 1
         /\ UNCHANGED <<usage, lease, controls, reservation, effect, unit,
                         resultIntent, remoteHead, terminalProof, observation,
                         submitIntent, submitOutcome, taskPlace, submitCalls>>

StartRunning == /\ dispatch = "launch-intent"
                /\ unit = "absent"
                /\ unit' = "running"
                /\ dispatch' = "running"
                /\ UNCHANGED <<usage, lease, controls, reservation, effect,
                                resultIntent, remoteHead, terminalProof, observation,
                                submitIntent, submitOutcome, taskPlace, starts, submitCalls>>

PrepareNaturalResult == /\ dispatch = "running"
                         /\ unit = "running"
                         /\ resultIntent' = TRUE
                         /\ remoteHead' = TRUE
                         /\ unit' = "success"
                         /\ dispatch' = "terminal"
                         /\ UNCHANGED <<usage, lease, controls, reservation, effect,
                                         terminalProof, observation, submitIntent,
                                         submitOutcome, taskPlace, starts, submitCalls>>

ObserveTerminal == /\ dispatch = "terminal"
                  /\ unit = "success"
                  /\ resultIntent
                  /\ remoteHead
                  /\ terminalProof' = TRUE
                  /\ observation' = TRUE
                  /\ UNCHANGED <<usage, lease, controls, reservation, effect,
                                  dispatch, unit, resultIntent, remoteHead,
                                  submitIntent, submitOutcome, taskPlace, starts, submitCalls>>

Settle == /\ dispatch = "terminal"
          /\ terminalProof
          /\ observation
          /\ resultIntent
          /\ remoteHead
          /\ effect = "held"
          /\ reservation = "reserved"
          /\ dispatch' = "settled"
          /\ effect' = "settled"
          /\ reservation' = "released"
          /\ UNCHANGED <<usage, lease, controls, unit, resultIntent, remoteHead,
                          terminalProof, observation, submitIntent, submitOutcome,
                          taskPlace, starts, submitCalls>>

PersistSubmitIntent == /\ dispatch = "settled"
                       /\ taskPlace = "working"
                       /\ lease = "live"
                       /\ submitIntent = FALSE
                       /\ submitIntent' = TRUE
                       /\ UNCHANGED <<usage, lease, controls, reservation, effect,
                                       dispatch, unit, resultIntent, remoteHead,
                                       terminalProof, observation, submitOutcome,
                                       taskPlace, starts, submitCalls>>

RelayResult == /\ dispatch = "settled"
              /\ submitIntent
              /\ lease = "live"
              /\ taskPlace = "working"
              /\ submitCalls < 1
              /\ submitCalls' = submitCalls + 1
              /\ submitOutcome' = "confirmed"
              /\ taskPlace' = "validating"
              /\ UNCHANGED <<usage, lease, controls, reservation, effect,
                              dispatch, unit, resultIntent, remoteHead,
                              terminalProof, observation, submitIntent, starts>>

LoseRelayAccepted == /\ dispatch = "settled"
                     /\ submitIntent
                     /\ lease = "live"
                     /\ taskPlace = "working"
                     /\ submitCalls = 0
                     /\ submitCalls' = 1
                     /\ taskPlace' = "validating"
                     /\ submitOutcome' = "unknown"
                     /\ UNCHANGED <<usage, lease, controls, reservation, effect,
                                     dispatch, unit, resultIntent, remoteHead,
                                     terminalProof, observation, submitIntent, starts>>

LoseRelayUnaccepted == /\ dispatch = "settled"
                       /\ submitIntent
                       /\ lease = "live"
                       /\ taskPlace = "working"
                       /\ submitCalls = 0
                       /\ submitOutcome' = "unknown"
                       /\ UNCHANGED <<usage, lease, controls, reservation, effect,
                                       dispatch, unit, resultIntent, remoteHead,
                                       terminalProof, observation, submitIntent,
                                       taskPlace, starts, submitCalls>>

ConfirmLostRelay == /\ submitOutcome = "unknown"
                   /\ taskPlace = "validating"
                   /\ submitCalls = 1
                   /\ submitOutcome' = "confirmed"
                   /\ UNCHANGED <<usage, lease, controls, reservation, effect,
                                   dispatch, unit, resultIntent, remoteHead,
                                   terminalProof, observation, submitIntent,
                                   taskPlace, starts, submitCalls>>

ReplayLostRelay == /\ submitOutcome = "unknown"
                   /\ taskPlace = "working"
                   /\ submitCalls = 0
                   /\ dispatch = "settled"
                   /\ submitIntent
                   /\ lease = "live"
                   /\ submitCalls' = 1
                   /\ taskPlace' = "validating"
                   /\ submitOutcome' = "confirmed"
                   /\ UNCHANGED <<usage, lease, controls, reservation, effect,
                                   dispatch, unit, resultIntent, remoteHead,
                                   terminalProof, observation, submitIntent, starts>>

LateUsage == /\ usage = "clear"
             /\ dispatch \in {"absent", "prepared"}
             /\ usage' = "unresolved"
             /\ UNCHANGED <<lease, controls, reservation, effect, dispatch, unit,
                             resultIntent, remoteHead, terminalProof, observation,
                             submitIntent, submitOutcome, taskPlace, starts, submitCalls>>

ExpireLease == /\ lease = "live"
              /\ submitCalls = 0
              /\ lease' = "expired"
              /\ UNCHANGED <<usage, controls, reservation, effect, dispatch, unit,
                              resultIntent, remoteHead, terminalProof, observation,
                              submitIntent, submitOutcome, taskPlace, starts, submitCalls>>

DisableControls == /\ controls = "enabled"
                   /\ controls' = "disabled"
                   /\ UNCHANGED <<usage, lease, reservation, effect, dispatch, unit,
                                   resultIntent, remoteHead, terminalProof, observation,
                                   submitIntent, submitOutcome, taskPlace, starts, submitCalls>>

Next == Reserve \/ Prepare \/ Begin \/ StartRunning \/ PrepareNaturalResult
        \/ ObserveTerminal \/ Settle \/ PersistSubmitIntent \/ RelayResult
        \/ LoseRelayAccepted \/ LoseRelayUnaccepted \/ ConfirmLostRelay
        \/ ReplayLostRelay \/ LateUsage \/ ExpireLease
        \/ DisableControls

Spec == Init /\ [][Next]_vars

ReservationHeldUntilTerminal == reservation = "released" => terminalProof
ExposureHeldUntilTerminal == effect = "settled" => terminalProof
ResultSubmittedAfterSettlement == submitCalls > 0 => dispatch = "settled"
SingleStart == starts <= 1
SingleSubmit == submitCalls <= 1
StartRequiresClearUsage == starts > 0 => usage = "clear"
SubmitRequiresLiveClaim == submitCalls > 0 => lease = "live"
ValidatingRequiresSubmit == taskPlace = "validating" => submitCalls = 1
====
