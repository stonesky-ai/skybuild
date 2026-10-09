---- MODULE EffectReconciliation ----
EXTENDS Naturals, FiniteSets
CONSTANTS Operations, UnsafeUnknownRelease
VARIABLES phase, observed, physical, held, recorded, accepted
vars == <<phase, observed, physical, held, recorded, accepted>>

(* Each stable operation belongs to the original definition. Replacement
   dispatch and usage settlement are outside this bounded safety model. *)
Init == /\ phase = "open"
        /\ observed = [o \in Operations |-> "absent"]
        /\ physical = [o \in Operations |-> "none"]
        /\ held = {}
        /\ recorded = {}
        /\ accepted = {}

Intent(o) == /\ phase = "open"
             /\ observed[o] = "absent"
             /\ observed' = [observed EXCEPT ![o] = "intent"]
             /\ held' = held \cup {o}
             /\ recorded' = recorded \cup {o}
             /\ UNCHANGED <<phase, physical, accepted>>

(* Dispatching durably retains uncertainty before external I/O. "flight"
   means an already issued request can still start after structural fencing. *)
Dispatch(o) == /\ phase = "open"
               /\ observed[o] = "intent"
               /\ observed' = [observed EXCEPT ![o] = "unknown"]
               /\ physical' = [physical EXCEPT ![o] = "flight"]
               /\ UNCHANGED <<phase, held, recorded, accepted>>

ExternalStart(o) == /\ physical[o] = "flight"
                    /\ physical' = [physical EXCEPT ![o] = "running"]
                    /\ UNCHANGED <<phase, observed, held, recorded, accepted>>
Acknowledge(o) == /\ observed[o] = "unknown"
                  /\ physical[o] = "running"
                  /\ observed' = [observed EXCEPT ![o] = "active"]
                  /\ UNCHANGED <<phase, physical, held, recorded, accepted>>
LoseObservation(o) == /\ observed[o] = "active"
                      /\ observed' = [observed EXCEPT ![o] = "unknown"]
                      /\ UNCHANGED <<phase, physical, held, recorded, accepted>>
ExternalFinish(o) == /\ physical[o] = "running"
                     /\ physical' = [physical EXCEPT ![o] = "ended"]
                     /\ UNCHANGED <<phase, observed, held, recorded, accepted>>

(* This is authoritative confirmation that an in-flight operation cannot
   start. A timeout, lease expiry or controller fence cannot take this step. *)
ConfirmNotStarted(o) == /\ physical[o] = "flight"
                        /\ physical' = [physical EXCEPT ![o] = "ended"]
                        /\ UNCHANGED <<phase, observed, held, recorded, accepted>>

RequestChange == /\ phase = "open"
                 /\ phase' = "pending"
                 /\ UNCHANGED <<observed, physical, held, recorded, accepted>>
CancelIntent(o) == /\ phase = "pending"
                   /\ observed[o] = "intent"
                   /\ observed' = [observed EXCEPT ![o] = "cancelled"]
                   /\ held' = held \ {o}
                   /\ UNCHANGED <<phase, physical, recorded, accepted>>
Reconcile(o) == /\ observed[o] \in {"unknown", "active"}
                /\ (physical[o] = "ended" \/
                    (UnsafeUnknownRelease /\ observed[o] = "unknown"))
                /\ observed' = [observed EXCEPT ![o] = "terminal"]
                /\ held' = held \ {o}
                /\ UNCHANGED <<phase, physical, recorded, accepted>>

(* Result evidence may advance only the still-current original definition.
   A terminal result can also be retained after pending/replaced without
   changing accepted; recorded already preserves its operation identity. *)
AcceptCurrent(o) == /\ phase = "open"
                    /\ observed[o] = "terminal"
                    /\ o \notin accepted
                    /\ accepted' = accepted \cup {o}
                    /\ UNCHANGED <<phase, observed, physical, held, recorded>>
Replace == /\ phase = "pending"
           /\ held = {}
           /\ phase' = "replaced"
           /\ UNCHANGED <<observed, physical, held, recorded, accepted>>

Next == RequestChange \/ Replace \/
        (\E o \in Operations : Intent(o) \/ Dispatch(o) \/ ExternalStart(o) \/
         Acknowledge(o) \/ LoseObservation(o) \/ ExternalFinish(o) \/
         ConfirmNotStarted(o) \/ CancelIntent(o) \/ Reconcile(o) \/ AcceptCurrent(o))

TypeOK == /\ phase \in {"open", "pending", "replaced"}
          /\ observed \in [Operations -> {"absent", "intent", "unknown", "active", "terminal", "cancelled"}]
          /\ physical \in [Operations -> {"none", "flight", "running", "ended"}]
          /\ held \subseteq Operations
          /\ recorded \subseteq Operations
          /\ accepted \subseteq recorded
UncertainHeld == \A o \in Operations : physical[o] \in {"flight", "running"} => o \in held
ReplacementSafe == phase = "replaced" => \A o \in Operations : physical[o] \notin {"flight", "running"}
RecordsRetained == \A o \in Operations : observed[o] # "absent" => o \in recorded
NoLateAcceptance == [][phase # "open" => UNCHANGED accepted]_vars
Spec == Init /\ [][Next]_vars
====
