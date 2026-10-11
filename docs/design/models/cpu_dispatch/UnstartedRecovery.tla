---- MODULE UnstartedRecovery ----
EXTENDS Naturals
CONSTANT Unsafe
VARIABLES state, launcherAlive, fenced, unit, held, releases
vars == <<state, launcherAlive, fenced, unit, held, releases>>
Init == /\ state = "launch-intent" /\ launcherAlive = TRUE
        /\ fenced = FALSE /\ unit = FALSE /\ held = TRUE /\ releases = 0
\* The launcher already holds start_once. A database change cannot stop it.
LateLaunch == /\ launcherAlive /\ ~fenced /\ ~unit
              /\ unit' = TRUE
              /\ UNCHANGED <<state, launcherAlive, fenced, held, releases>>
StopLauncher == /\ launcherAlive /\ launcherAlive' = FALSE
                /\ UNCHANGED <<state, fenced, unit, held, releases>>
FenceLauncher == /\ ~launcherAlive /\ ~fenced /\ fenced' = TRUE
                 /\ UNCHANGED <<state, launcherAlive, unit, held, releases>>
Recover == /\ state = "launch-intent" /\ ~unit
           /\ (Unsafe \/ (~launcherAlive /\ fenced))
           /\ state' = "cancelled" /\ held' = FALSE /\ releases' = releases + 1
           /\ UNCHANGED <<launcherAlive, fenced, unit>>
RepeatRecover == /\ state = "cancelled" /\ UNCHANGED vars
Next == LateLaunch \/ StopLauncher \/ FenceLauncher \/ Recover \/ RepeatRecover
Spec == Init /\ [][Next]_vars
NoLiveWorkAfterRelease == ~held => ~unit
ReleaseOnce == releases <= 1
NoRevival == state = "cancelled" => ~held
====
