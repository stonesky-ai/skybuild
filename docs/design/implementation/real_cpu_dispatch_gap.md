# Reusable CPU dispatch and settlement gap

The bounded two-worker permit route demonstrates automatic Ready-task selection,
fenced claims, durable local launch, and result delivery for administrator-approved
deterministic patches. It does not redeem `reserve_cpu`: the current reservation
API intentionally has no dispatch transition or runtime binding, and its cancel
path applies only to never-dispatched work. A pool status read is also not an
atomic reservation. The one-shot route therefore keeps uncertainty and cannot
be used as a reusable admission service.

The reusable backend needs a durable dispatch record keyed by project, task,
fenced attempt, CPU reservation, reviewed source head, execution profile,
approved limits, and job-unit identity. An owner-authorized transaction must
atomically move a held reservation into a dispatched state and pin the claim
fence before the trusted launcher receives permission to start. The launcher
must write a unit launch intent once, call `JobUnitManager.start` once, and
record the invocation ID. An unknown launch acknowledgement retains the
reservation and blocks another attempt until invocation-bound reconciliation.

Settlement needs an exact terminal unit observation, fenced REST result state,
and an idempotent compare-and-swap transition. It releases the held units only
after the terminal observation belongs to the pinned invocation. Lost unit
state, timeout, restart, or an uncertain stop retains exposure and requires
owner reconciliation. Central or local disablement prevents fresh dispatch;
it must not be represented as a physical stop of an already running unit.

Add a service migration and owner routes for prepare, dispatch, observe, and
settle; add `Client` methods and a trusted launcher adapter that uses the
existing `JobUnitManager`. Exercise duplicate requests, crashes before and
after unit start, conflicting fences, replacement units, disablement, and
terminal settlement against a disposable database and fake systemd. Generic
model-authored execution also needs a separate credential and filesystem
boundary plus isolated candidate tests. Neither is qualified by this CPU
patch route.
