# CPU worker terminal relay model

`CPUWorkerRelay.tla` models the production ordering added by the reusable
CPU worker bridge. One reservation and one held effect bind one claim fence.
The usage guard is checked again at one-shot begin, because unresolved lineage
usage may arrive after reservation. A worker persists the exact result intent
and verifies its remote head before the trusted controller records terminal
proof. Only then may settlement release the existing effect and reservation.
The owner relay submits the same fenced result after settlement; an uncertain
reply is resolved from workflow state/history under the saved idempotency key.

The finite model covers one unit and one task, with late usage scoped to the
admission window between reservation and launch authorization. Lease expiry
before relay blocks submission. The model does not establish liveness, real
database locking, HTTP behavior, source/profile provenance, systemd identity,
or physical process termination. Those require the source and PostgreSQL
checks plus separately approved runtime qualification.

The finite configuration is `CPUWorkerRelay.cfg`. Run SANY and bounded TLC
only under the serialized, memory-capped Wonko gate plan; do not run an
unbounded local model check.
