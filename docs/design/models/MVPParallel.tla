--------------------------- MODULE MVPParallel ---------------------------
EXTENDS Admission

\* A deliberately false invariant supplies a reachable parallel-work witness.
SerialOnly == Cardinality({w \in Workers : phase[w] = "redeemed"}) <= 1
=============================================================================
