---- MODULE CPUWorkerDispatch ----
EXTENDS Naturals

VARIABLES dispatch, reservation, effect, controlsEnabled, leaseLive, approvalLive,
          launchAuthorized, startCount, unitInvocation, pinnedInvocation,
          observedInvocation, unitTerminal, resultVerified, taskPhase
vars == <<dispatch, reservation, effect, controlsEnabled, leaseLive, approvalLive,
          launchAuthorized, startCount, unitInvocation, pinnedInvocation,
          observedInvocation, unitTerminal, resultVerified, taskPhase>>

Init ==
    /\ dispatch = "none"
    /\ reservation = "held"
    /\ effect = "uncreated"
    /\ controlsEnabled = TRUE
    /\ leaseLive = TRUE
    /\ approvalLive = TRUE
    /\ launchAuthorized = FALSE
    /\ startCount = 0
    /\ unitInvocation = "none"
    /\ pinnedInvocation = "none"
    /\ observedInvocation = "none"
    /\ unitTerminal = FALSE
    /\ resultVerified = FALSE
    /\ taskPhase = "working"

Prepare ==
    /\ dispatch = "none"
    /\ reservation = "held"
    /\ effect = "uncreated"
    /\ controlsEnabled
    /\ leaseLive
    /\ approvalLive
    /\ taskPhase = "working"
    /\ dispatch' = "prepared"
    /\ effect' = "held"
    /\ UNCHANGED <<reservation, controlsEnabled, leaseLive, approvalLive,
                    launchAuthorized, startCount, unitInvocation, pinnedInvocation,
                    observedInvocation, unitTerminal, resultVerified, taskPhase>>

Begin ==
    /\ dispatch = "prepared"
    /\ controlsEnabled
    /\ leaseLive
    /\ approvalLive
    /\ launchAuthorized' = TRUE
    /\ dispatch' = "launch-intent"
    /\ UNCHANGED <<reservation, effect, controlsEnabled, leaseLive, approvalLive,
                    startCount, unitInvocation, pinnedInvocation, observedInvocation,
                    unitTerminal, resultVerified, taskPhase>>

ExpireOrDisable ==
    /\ approvalLive
    /\ approvalLive' = FALSE
    /\ controlsEnabled' = FALSE
    /\ UNCHANGED <<dispatch, reservation, effect, leaseLive, launchAuthorized,
                    startCount, unitInvocation, pinnedInvocation, observedInvocation,
                    unitTerminal, resultVerified, taskPhase>>

LoseLease ==
    /\ leaseLive
    /\ leaseLive' = FALSE
    /\ UNCHANGED <<dispatch, reservation, effect, controlsEnabled, approvalLive,
                    launchAuthorized, startCount, unitInvocation, pinnedInvocation,
                    observedInvocation, unitTerminal, resultVerified, taskPhase>>

StartOnce ==
    /\ dispatch = "launch-intent"
    /\ launchAuthorized
    /\ startCount = 0
    /\ startCount' = 1
    /\ unitInvocation' = "inv1"
    /\ pinnedInvocation' = "inv1"
    /\ dispatch' = "running"
    /\ UNCHANGED <<reservation, effect, controlsEnabled, leaseLive, approvalLive,
                    launchAuthorized, observedInvocation, unitTerminal,
                    resultVerified, taskPhase>>

LoseStartReply ==
    /\ dispatch = "running"
    /\ unitInvocation = pinnedInvocation
    /\ dispatch' = "unknown"
    /\ UNCHANGED <<reservation, effect, controlsEnabled, leaseLive, approvalLive,
                    launchAuthorized, startCount, unitInvocation, pinnedInvocation,
                    observedInvocation, unitTerminal, resultVerified, taskPhase>>

ObserveInvocation ==
    /\ dispatch \in {"running", "unknown"}
    /\ unitInvocation = pinnedInvocation
    /\ observedInvocation' = pinnedInvocation
    /\ dispatch' = "running"
    /\ UNCHANGED <<reservation, effect, controlsEnabled, leaseLive, approvalLive,
                    launchAuthorized, startCount, unitInvocation, pinnedInvocation,
                    unitTerminal, resultVerified, taskPhase>>

NaturalTerminal ==
    /\ dispatch \in {"running", "unknown"}
    /\ unitInvocation = pinnedInvocation
    /\ pinnedInvocation # "none"
    /\ unitTerminal = FALSE
    /\ unitTerminal' = TRUE
    /\ UNCHANGED <<dispatch, reservation, effect, controlsEnabled, leaseLive, approvalLive,
                    launchAuthorized, startCount, unitInvocation, pinnedInvocation,
                    observedInvocation, resultVerified, taskPhase>>

ObserveVerifiedTerminal ==
    /\ dispatch \in {"running", "unknown"}
    /\ unitTerminal
    /\ unitInvocation = pinnedInvocation
    /\ pinnedInvocation # "none"
    /\ observedInvocation' = pinnedInvocation
    /\ resultVerified' = TRUE
    /\ dispatch' = "terminal"
    /\ UNCHANGED <<reservation, effect, controlsEnabled, leaseLive, approvalLive,
                    launchAuthorized, startCount, unitInvocation, pinnedInvocation,
                    unitTerminal, taskPhase>>

ReplacementUnit ==
    /\ dispatch \in {"running", "unknown", "terminal"}
    /\ unitInvocation = "inv1"
    /\ unitInvocation' = "inv2"
    /\ dispatch' = "unknown"
    /\ UNCHANGED <<reservation, effect, controlsEnabled, leaseLive, approvalLive,
                    launchAuthorized, startCount, pinnedInvocation, observedInvocation,
                    unitTerminal, resultVerified, taskPhase>>

TaskAdvancesToValidation ==
    /\ taskPhase = "working"
    /\ resultVerified
    /\ taskPhase' = "validating"
    /\ UNCHANGED <<dispatch, reservation, effect, controlsEnabled, leaseLive,
                    approvalLive, launchAuthorized, startCount, unitInvocation,
                    pinnedInvocation, observedInvocation, unitTerminal, resultVerified>>

Settle ==
    /\ dispatch = "terminal"
    /\ unitTerminal
    /\ resultVerified
    /\ observedInvocation = pinnedInvocation
    /\ pinnedInvocation = unitInvocation
    /\ reservation = "held"
    /\ effect = "held"
    /\ dispatch' = "settled"
    /\ reservation' = "released"
    /\ effect' = "settled"
    /\ UNCHANGED <<controlsEnabled, leaseLive, approvalLive, launchAuthorized,
                    startCount, unitInvocation, pinnedInvocation, observedInvocation,
                    unitTerminal, resultVerified, taskPhase>>

RetryStart ==
    /\ dispatch = "unknown"
    /\ startCount' = startCount + 1
    /\ unitInvocation' = "inv2"
    /\ dispatch' = "running"
    /\ UNCHANGED <<reservation, effect, controlsEnabled, leaseLive, approvalLive,
                    launchAuthorized, pinnedInvocation, observedInvocation,
                    unitTerminal, resultVerified, taskPhase>>

Next == Prepare \/ Begin \/ ExpireOrDisable \/ LoseLease \/ StartOnce \/ LoseStartReply
        \/ ObserveInvocation \/ NaturalTerminal \/ ObserveVerifiedTerminal
        \/ ReplacementUnit \/ TaskAdvancesToValidation \/ Settle

BrokenNext == Next \/ RetryStart

AtMostOneStart == startCount <= 1
ReleaseRequiresExactNaturalTerminal ==
    reservation = "released" =>
        /\ dispatch = "settled"
        /\ effect = "settled"
        /\ unitTerminal
        /\ resultVerified
        /\ observedInvocation = pinnedInvocation
        /\ pinnedInvocation = unitInvocation
NoStartWithoutCommittedAuthorization == startCount > 0 => launchAuthorized
UnresolvedExposureHeld ==
    dispatch \in {"launch-intent", "running", "unknown", "terminal"} =>
        /\ reservation = "held"
        /\ effect = "held"
TaskValidationDoesNotBlockSettlement ==
    taskPhase = "validating" /\ dispatch = "terminal" => reservation = "held"

Spec == Init /\ [][Next]_vars
BrokenSpec == Init /\ [][BrokenNext]_vars
====
