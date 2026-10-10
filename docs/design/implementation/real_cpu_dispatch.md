# Real CPU worker dispatch

The real CPU worker path composes the existing `cpu_reservations` capacity
ledger with one immutable `task_effects` record. Migration 014 adds the local
unit identity and append-only observations needed to prove a natural terminal
result. A committed launch intent permits one `JobUnitManager.start` call;
unknown start or terminal state keeps both effect exposure and reservation
held. Only an exact host, unit, launch nonce, InvocationID, successful terminal
observation, and verified `submitted.json` digest can settle and release the
reservation. Settlement does not require the task to remain at its original
revision, so a later Validating transition cannot strand confirmed terminal
capacity.

The only profile is the reviewed deterministic auto-patch worker. It receives
fixed arguments assembled by the trusted bridge and never runs candidate code
or tests. The bridge pins private assignment, preclaim, patch, permit,
interpreter, and worker source inputs before prepare and rechecks them before
launch. It does not request or claim physical stop capability.

Before each API mutation or local start, the bridge compares the loaded bridge,
dispatch, Client, contracts, and JobUnitManager modules with bytes at a clean
controller Git HEAD. It also requires an owner-controlled, mode-0600 profile
manifest outside the controller checkout and private worker attempt state. The
worker import checkout must equal the verified controller source root; the
candidate clone is made separately under private attempt state. The bridge
constructs the exact `JobUnitManager` with Python's real `subprocess.run`; its
launch and reconcile APIs accept no caller-supplied process witness. The
manifest schema is
`skybuild.cpu-worker-controller-profile.v1` and has exactly these fields:

```json
{
  "schema": "skybuild.cpu-worker-controller-profile.v1",
  "profile_id": "bounded-trusted-cpu-patch-v1",
  "controller_head": "<reviewed 40-character Git commit>",
  "controller_files": {
    "scripts/skybuild_job_unit.py": "<SHA-256>",
    "src/skybuild/__init__.py": "<SHA-256>",
    "src/skybuild/client.py": "<SHA-256>",
    "src/skybuild/contracts.py": "<SHA-256>",
    "src/skybuild/cpu_worker_bridge.py": "<SHA-256>",
    "src/skybuild/cpu_worker_dispatch.py": "<SHA-256>"
  },
  "interpreter_sha256": "<SHA-256 of the trusted Python interpreter>"
}
```

The manifest must be provisioned from an independently reviewed source head;
the bridge refuses an absent, malformed, stale, or mismatched manifest. No
manifest provisioning or live launch is part of this source change. This branch
does not contain the reviewed auto-worker module set; its source checks remain
default-deny until the exact reviewed worker files are installed and the
combined `client.py` matches the pinned digest. Same-UID tampering is outside
this boundary. The finite TLA+ model covers one action,
one invocation, a replacement unit, terminal observation, and task advancement;
TLC execution remains pending a bounded remote run.
