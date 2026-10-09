# Optional durable disposable gate reporting

Canonical task: `SKYBUILD-BUNDLED-INTEGRATION`. Supporting reporting scope for
execution controls and self-build delivery. Source base:
`e85c22451a517bca94d780eca325f6f0fd4759ee`. This bounded change implements the
gate-reporting recommendation in the owner's MVP gap audit. Architecture A38's
working contract and bundled integration section, implementation plan 5a, and
the existing disposable PostgreSQL skill govern. No architecture, acceptance,
publication or runtime authority changes are proposed.

The existing `scripts/disposable_pg_gate.py` gains four optional arguments,
required together: `--artifact`, `--run-id`, `--expected-head`, and
`--expected-tree`. Omitting all four preserves its default CLI and stdout result
shape. Supplying them binds a single invocation to an explicit clean checkout,
full commit and tree identifiers. Use an absolute output path in an existing,
owned mode-0700 directory outside that checkout. Existing output always refuses,
including an identical run ID; this is reporting, never retry or resume.

Example addition to the existing guarded invocation:

```sh
nice -n 10 python scripts/disposable_pg_gate.py \
  --checkout /absolute/frozen-candidate --min-available-gib 10 \
  --artifact /private/gate-artifacts/bundle-001.json --run-id bundle-001 \
  --expected-head FULL_REVIEWED_COMMIT --expected-tree FULL_FROZEN_TREE
```

The mode-0600 initial record is exclusively created and synchronized before
Docker. It binds the candidate, invocation UUID/PID, command-argv and image-reference digests,
timeout/monotonic deadline, memory floor, log and owned random container name.
Meaningful stages are prepared, starting PostgreSQL, waiting for PostgreSQL,
creating databases, testing, cleanup and terminal. Updates use a private
temporary file, file fsync, atomic replace and directory fsync. Each update
requires the prior record bytes; changed evidence is never adopted. This is a
single cooperating writer contract, not protection against a hostile same-user
process racing local filesystem operations.

Terminal reporting distinguishes test failure, timeout, interruption, ordinary
error, changed/unreadable candidate and unconfirmed cleanup. Cleanup reports
`not_started`, `unknown`, or `confirmed`; a pre-effect record conservatively
marks cleanup unknown before attempting Docker. Container removal retains the
existing owned-name, volume cleanup and 30-second timeout. Candidate pins and
cleanliness are rechecked before container creation, before tests and before
terminal acceptance. These snapshots do not lock the checkout against a hostile
concurrent writer; the frozen-candidate ownership contract still applies.

If reporting fails, stdout cannot report success. A progress failure may still
permit a durable terminal error after owned cleanup; an unpersistable terminal
leaves the last stage incomplete. Initial fsync failure may leave an incomplete
reserved output. Neither an incomplete artifact nor a local timeout proves the
process stopped or cleanup completed, and neither permits restarting the run.
Preserve the artifact and log for operator reconciliation. Ordinary stdout
remains the existing gate result, with optional run/artifact references and a
fixed artifact-error indicator when reporting cannot be confirmed.

The artifact contains no DSN, password, environment, raw command/image string,
subprocess output or pytest summary. The existing private log and password
redaction remain unchanged. The record reports this invocation; it is not a
trusted gate acceptance token or publication authority. No new runner, service,
timer, polling loop, inference operation, task state mutation or automatic
publication is added. Existing PostgreSQL limits, deadline, memory checks and
default gate workflow remain in place.

Focused tests use a fake subprocess runner, controlled clock and private scratch
artifacts, plus a tiny local Git repository for candidate identity. They cover
effect ordering, exact pins, default output, conflict/reuse, source drift,
interruption/timeout, failed cleanup, private paths, lost writes and retained
foreign evidence. No real Docker, database or full combined gate is run by this
task. Parent independently reviews the exact pushed head and later owns its
normal frozen-bundle gate.

Author validation: `tests/test_disposable_pg_gate.py` produced **25 passed in
0.17 seconds** with the existing project interpreter and this checkout's
absolute `src` PYTHONPATH. Package import origin, CLI help and `git diff --check`
were verified. An initial fake-runner assertion tried to parse intentionally
foreign artifact content during cleanup; restricting that assertion to the
relevant simulated execution stages corrected the test harness. No production
effect was involved. Independent exact-head review remains pending.
