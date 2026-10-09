--------------------------- MODULE Publication ---------------------------
EXTENDS Naturals, FiniteSets

CONSTANTS Bundles, MaxBase, MaxGeneration,
          BrokenRemoteGuard, BrokenFence, BrokenRelease
VARIABLES target, generation, ready, base, fence, sent, outcome,
          reconciled, held, published, dispatches
vars == <<target, generation, ready, base, fence, sent, outcome,
          reconciled, held, published, dispatches>>

Init ==
    /\ target = 0
    /\ generation = 0
    /\ ready = {}
    /\ base = [b \in Bundles |-> 0]
    /\ fence = [b \in Bundles |-> 0]
    /\ sent = {}
    /\ outcome = [b \in Bundles |-> "unsent"]
    /\ reconciled = {}
    /\ held = {}
    /\ published = {}
    /\ dispatches = {}

Gate(b) ==
    /\ b \notin ready
    /\ ready' = ready \cup {b}
    /\ base' = [base EXCEPT ![b] = target]
    /\ fence' = [fence EXCEPT ![b] = generation]
    /\ UNCHANGED <<target, generation, sent, outcome, reconciled, held,
                    published, dispatches>>

Dispatch(b) ==
    /\ b \in ready \ sent
    /\ held = {}
    /\ base[b] = target
    /\ (BrokenFence \/ fence[b] = generation)
    /\ sent' = sent \cup {b}
    /\ held' = {b}
    /\ outcome' = [outcome EXCEPT ![b] = "pending"]
    /\ dispatches' = dispatches \cup
           {[bundle |-> b, expected |-> fence[b], actual |-> generation]}
    /\ UNCHANGED <<target, generation, ready, base, fence, reconciled,
                    published>>

RemoteApply(b) ==
    /\ outcome[b] = "pending"
    /\ target < MaxBase
    /\ (BrokenRemoteGuard \/ base[b] = target)
    /\ published' = published \cup
           {[bundle |-> b, expected |-> base[b], actual |-> target]}
    /\ target' = target + 1
    /\ outcome' = [outcome EXCEPT ![b] = "applied"]
    /\ UNCHANGED <<generation, ready, base, fence, sent, reconciled, held,
                    dispatches>>

RemoteReject(b) ==
    /\ outcome[b] = "pending"
    /\ ~BrokenRemoteGuard
    /\ base[b] # target
    /\ outcome' = [outcome EXCEPT ![b] = "rejected"]
    /\ UNCHANGED <<target, generation, ready, base, fence, sent, reconciled,
                    held, published, dispatches>>

Reconcile(b) ==
    /\ b \in held
    /\ outcome[b] \in {"applied", "rejected"}
    /\ reconciled' = reconciled \cup {b}
    /\ held' = held \ {b}
    /\ UNCHANGED <<target, generation, ready, base, fence, sent, outcome,
                    published, dispatches>>

ReleaseUnknown(b) ==
    /\ BrokenRelease
    /\ b \in held
    /\ outcome[b] = "pending"
    /\ held' = held \ {b}
    /\ UNCHANGED <<target, generation, ready, base, fence, sent, outcome,
                    reconciled, published, dispatches>>

OtherWriter ==
    /\ target < MaxBase
    /\ target' = target + 1
    /\ UNCHANGED <<generation, ready, base, fence, sent, outcome,
                    reconciled, held, published, dispatches>>

ReplaceOwner ==
    /\ generation < MaxGeneration
    /\ generation' = generation + 1
    /\ UNCHANGED <<target, ready, base, fence, sent, outcome,
                    reconciled, held, published, dispatches>>

Idle == UNCHANGED vars
Next == OtherWriter \/ ReplaceOwner \/ Idle \/
        (\E b \in Bundles : Gate(b) \/ Dispatch(b) \/ RemoteApply(b) \/
         RemoteReject(b) \/ Reconcile(b) \/ ReleaseUnknown(b))
Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ target \in 0..MaxBase
    /\ generation \in 0..MaxGeneration
    /\ ready \subseteq Bundles
    /\ base \in [Bundles -> 0..MaxBase]
    /\ fence \in [Bundles -> 0..MaxGeneration]
    /\ sent \subseteq ready
    /\ outcome \in [Bundles -> {"unsent", "pending", "applied", "rejected"}]
    /\ reconciled \subseteq sent
    /\ held \subseteq sent
SingleOutstanding == Cardinality(held) <= 1
UnreconciledHeld == sent \ reconciled = held
ExactBaseAtPublication == \A p \in published : p.expected = p.actual
CurrentFenceAtDispatch == \A d \in dispatches : d.expected = d.actual
=============================================================================
