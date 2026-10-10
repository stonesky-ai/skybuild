# Isolated Full-Test Gate

## Purpose

`scripts/isolated_full_test_gate.py` runs the unchanged full-suite command from
`skybuild.manual_integration` against an exact candidate Git archive. The host
process supervises Docker and writes the final receipt. Candidate tests never
receive the Docker socket, host home, host credentials, or writable host paths.

The candidate command and digest remain:

```text
uv run --extra test python -m pytest -q
```

The runner uses the pinned local test image with `UV_OFFLINE=1` and
`UV_NO_SYNC=1`. Image labels bind its dependencies to the candidate's
`pyproject.toml`, `uv.lock`, and frozen gate policy. Docker uses
`--pull=never`; missing images stop before execution.

The image also pins uv version `0.11.22` with label
`org.skybuild.full-test.uv-version`. It supplies installed dependencies and
static SkyBuild editable metadata beneath `/opt/skybuild-venv`, including
`.skybuild-environment.json` with exactly these fields: schema
`skybuild.full-test.environment.v1`, `uv_version`, `pyproject_sha256`, and
`uv_lock_sha256`. The project and lock hashes must match the candidate bytes.
Image preparation installs approved dependency wheels and constructs root
project metadata as data; it must never execute candidate build backends or
hooks in a credentialed host or signing context.

`scripts/build_isolated_gate_images.py` builds the local runner and firewall
images used by this supervisor. It accepts only the pinned local Python base
image ID `sha256:cae66f2ef0ec51a9891263eeee7f987dacf0a9879e8aa9353d5606e0530619a5`
and PostgreSQL base image ID
`sha256:1a6ab3f5345eb6dbe04a1349529caabdb0ab09293a09590fad07b2246bfa4b54`.
The runner uses the pinned uv 0.11.22 binary and exact project lock with
`--offline --no-build --no-install-project`; the builder context contains only
the project metadata, lock, uv binary, trusted metadata writer, and pinned runner tool
payload. Dependency preparation runs in a disposable, resource-limited
container with no network, mounting only that context read-only, the local uv
cache, and a temporary output directory. It never pulls an image or builds a
candidate package. The final runner image starts from the exact Python base
filesystem in a clean `scratch` stage so inherited environment values do not
bypass the candidate allowlist.

The runner base needs extra tools for the unchanged suite. Local repository
fixtures use Git. Session scans use ripgrep. DNS supervisor tests compile C.
TLS configuration tests use the Docker CLI and Compose without a daemon socket. `scripts/gate_images/trusted_git_packages.json` pins the
exact Git, ripgrep, GCC, C headers, linker, Docker CLI, Compose, and their
required packages from Debian trixie by
version and SHA-256. The packages were resolved from the pinned base's signed
APT repository metadata and are unpacked from a local package directory; the
image builder and candidate runtime do not use network access or package
managers. The final image includes the Git executable, its core helpers,
templates, and package runtime files, while its immutable image ID pins the
complete staged payload. The builder installs the `cc` alias explicitly because
archive extraction does not run Debian package scripts. Before accepting the
image, the builder runs the tools and compiles and executes a small C program
with no network, no Docker socket, and the candidate privilege restrictions.

The firewall image stages only the host's pinned nftables `iptables` multicall
binary, conntrack matcher, and their non-glibc shared libraries. Its payload
manifest records every staged file hash; the expected predicate pins the final
immutable image ID. The helper installs rules only inside the disposable
container network namespace. Its 1 MiB `/run` tmpfs provides the xtables lock
file while the helper root filesystem remains read-only. Image construction is
an explicit `--build` operation and must follow independent source review and
owner approval. Missing offline wheels or mismatched local image/tool digests
stop preparation before the full gate starts.

## Candidate boundary

The supervisor verifies a clean trusted checkout and candidate checkout, the
candidate commit and tree, the target-base ancestry, and the raw
`git archive --format=tar` SHA-256. A kernel file-size limit bounds archive
production to 512 MiB before the producer can fill host storage; production
also has a 120-second timeout. It rejects unsafe archive members and
extracts source into a private run directory. The candidate container receives
that archive read-only, one bounded scratch tmpfs, and two read-only trusted
runner fixtures. It runs as UID 10001 with a read-only root filesystem,
`CAP_DROP=ALL`, no-new-privileges, and explicit memory, CPU, PID, and wall-clock
limits.
Docker's local log driver caps retained output at 128 MiB for the test
container, 32 MiB for PostgreSQL, and 4 MiB for each helper container.

The container environment is an exact allowlist. Three synthetic DSNs point to
three databases in one disposable PostgreSQL container. No live application
DSN or user credential enters the candidate. The entrypoint emits a blocked
marker, waits for the supervisor release file, and only then copies the source
archive to scratch, verifies the source package path without importing candidate
code, checks connectivity denial, and executes the unchanged full test command.
The trusted Python launcher starts in isolated mode. No candidate module runs
before the fixed command exec. Scratch permits executable test fixtures while
retaining its 2 GiB tmpfs size, UID, nosuid, nodev, and resource limits.

The trusted launcher copies at most 256 MiB and 100,000 entries from the
preinstalled image environment into scratch `.venv`. Both the full command and
the unchanged project launcher use that writable copy. The launcher rebases
editable source URL/path and uv's cache metadata as data. This is necessary
because an existing test invokes `scripts/project_python`, clears
`UV_NO_SYNC`, and supplies an empty cache. The pinned uv protocol records file
ctime and source-directory creation time (or inode when creation time is
unavailable). The copied cache metadata must reflect those new filesystem
identities, so nested uv can audit the existing installation without building
or downloading packages. The image requires GNU `/usr/bin/stat` for this
nonexecuting metadata inspection. Dynamic project metadata, custom uv cache
keys, and setup.py/setup.cfg require separate environment qualification.

The protocol follows uv's [file timestamp implementation](https://github.com/astral-sh/uv/blob/main/crates/uv-cache-info/src/timestamp.rs)
and [directory cache implementation](https://github.com/astral-sh/uv/blob/main/crates/uv-cache-info/src/cache_info.rs),
and must be checked against the exact installed uv version with the focused
offline regression before image qualification. Changing uv requires a new
protocol review. No candidate module or build backend runs during this
preparation.

Existing ledger and promotion tests require three frozen source commits:
`6d96075f88493d0b54577a2a8c9526f19a78a5ed`,
`d79d2e1947d2c8e9edb577ab5f5093edfa3c94e3`, and
`7d40df9fa7b26035736ffa613b5c5dad548269f5`. The supervisor constructs a shallow
Git fixture containing only those commits and their trees/blobs. Its pack has
a 64 MiB kernel write bound. The fixture receives synthetic HEAD, refs, shallow
metadata, and a minimal config; host Git config, hooks, alternates, credentials,
and remotes are never copied. It lives in `.git` beneath the existing read-only
`/candidate` mount. Scratch `.git` links to that read-only fixture; scratch HOME
contains only a generated Git safe.directory entry for the scratch workspace.

`candidate_history_sha256` independently pins the entire fixture. Its digest
is SHA-256 of canonical compact JSON mapping each relative fixture filename to
the SHA-256 of its raw bytes, including the pack, index, and synthetic metadata.
Preparation and execution reconstruct and compare that digest. The plan also
records the fixed commit list, pack size, size bound, and read-only requirement.
The trusted expected predicate must supply the approved digest.

## Network isolation

The runner creates one internal IPv4 Docker network without masquerading. A
pinned firewall image runs as a short-lived `NET_ADMIN` sidecar in each
container network namespace. The candidate and PostgreSQL containers retain
`CAP_DROP=ALL`; no host firewall rule changes.

Both namespace policies set IPv4 and IPv6 INPUT, OUTPUT, and FORWARD defaults
to DROP. Candidate IPv4 OUTPUT allows only the exact PostgreSQL address on TCP
5432 plus established replies. PostgreSQL IPv4 INPUT allows only the exact
candidate address on TCP 5432 plus established replies. Both namespaces allow
loopback and established traffic. Traffic to Docker's embedded resolver address
is denied on every port before loopback acceptance, including ports rewritten
by Docker's resolver NAT. The runner compares complete filter-table output from
`iptables-save -t filter` and `ip6tables-save -t filter` to the policy before release.

Trusted probes run in both namespaces before candidate release. They require
DNS, external IPv4, external IPv6, and an owned unexpected-port host listener
to be unreachable. The candidate probe also requires PostgreSQL TCP 5432 to
work. A separate `pg_isready` call uses the PostgreSQL container's local Unix
socket before the ingress firewall is installed. This preserves test fixture
superuser semantics while blocking PostgreSQL `COPY PROGRAM` from pivoting to
the host or the Internet.

PostgreSQL uses a read-only root filesystem and bounded tmpfs mounts for data,
its Unix socket directory, and `/tmp`. The data tmpfs is 1 GiB. Docker inspect
must confirm all mount destinations, size limits, image IDs, container IDs,
resource caps, network membership, firewall rules, and fixture mounts.

## Plan and receipt

Preparation is read-only. It emits a canonical plan binding the candidate
archive, command, trusted source hashes, immutable local images, dual-stack
firewall policy, environment and mount allowlists, and resource limits. Actual
execution requires a separate GO record binding that plan digest, exact runner
source, image IDs, signer hash, execution host, pinned key ID, and distinct
reviewer and root authorizer identities.

The host journal records create intent, full resource IDs, firewall and probe
results, candidate exit status, and independent cleanup confirmations. Logs
are redacted for the synthetic PostgreSQL password and hashed by the host.
The host signs the final receipt with the pinned HMAC key only after it observes
the candidate exit and cleanup results. Any supervisor exception forces a failed
exit and prevents signing. CLI success also requires a written attestation and
no supervisor failure. Uncertain cleanup remains an explicit failure.

The helper imports `scripts/trusted_gate_attestation.py`; its raw SHA-256 is
bound by the expected predicate, clean trusted checkout, plan, and GO record.
The key must be an owned regular file with mode 0600 and 32–4096 bytes. The key
is never mounted into a container.

## Execution status

Focused tests cover policy binding, environment and mount rejection, bounded
archive production, deterministic sanitized history and local Git fetch,
hostile pre-exec imports, firewall rule parsing, key checks, cleanup
reconciliation, chunk-boundary log redaction, and the unchanged project launcher
using copied installed dependencies with an empty offline cache. A Docker model interprets the
actual builder arguments and feeds the actual inspect verifiers. The modeled
execution signs and verifies its real assembled receipt, and rejects deliberate
IP, helper-label, log-format, preflight, test-exit, cleanup, and receipt changes.
These checks do not prove Docker behavior. Do not claim a full-suite pass
until an independent source review approves the exact runner commit and root
issues GO for the reviewed plan and resource host. The actual isolated runtime
rehearsal is still required for qualification.

## One-use owner policy

`--execute-policy` is separate from manual `--execute --reviewed-go-record`.
It requires private `--policy-permit`, `--policy-trust`, and `--policy-input`
files, plus exact raw-byte SHA-256 pins for the permit and trust configuration.
All authorization files and keys must be owned regular files with mode 0600,
outside the candidate checkout. The state directory must be owned mode 0700.
The existing attestation key, output directory, candidate checkout, and expected
predicate arguments remain required. Policy mode cannot accept a manual GO.

The owner approval, trusted integration collector, and independent reviewers
use separate configured principals and keys. Their envelopes contain exactly
`schema`, `key_id`, `principal`, `payload`, and `signature`. The signature is
lowercase HMAC-SHA256 over the schema UTF-8 bytes, one NUL byte, and canonical
payload JSON. Canonical JSON sorts keys, uses compact separators, emits UTF-8,
and rejects nonfinite values. Duplicate and unknown fields are rejected.
Key paths come only from the hash-pinned owner trust configuration.

The owner permit approves exactly two assignments, workers, branches, brief
digests, immutable patch files, disjoint exact owned paths, definition revisions,
policy versions, and one frozen base. It also pins trusted source provenance,
the policy module hash, immutable images, the command/profile, host, resource
limits, signer identity, weekly usage observation, expiry, and maximum one run.
Its delivery section binds conductor, integration, and publisher source heads,
publisher and gate trust digests, the exact PR source branch, and private state
root. The signed integration input supplies the later frozen heads, workflow
tuples, actual independent review artifact digests, candidate identity, prepared
manifest digest, and real PR tuple. This input cannot choose keys or broaden the
owner policy. `scripts/gate_policy.py` defines the exact versioned field sets.

Each stage has a distinct immutable intent under the same permit ID. The
conductor creates and fsyncs its intent before its first write. The gate verifies
the conductor intent and creates its own intent with O_EXCL, fsyncs the file and
directory, and then prepares candidate source. Re-signing the same permit ID
cannot grant another gate run. Incomplete preparation, failed execution, unknown
cleanup, and interrupted runs retain consumption. Reconciliation cannot delete
an intent or replay an effect. Later publisher and acceptance stages must apply
their own reviewed one-use records linked to prior immutable stage digests.

The gate independently checks signed reviewer principals and exact workflow
tuples, hashes actual review artifacts and approved patch bytes, and reconstructs
each worker tree using a private Git index and object directory. Each worker is
one commit on the approved base. The candidate must contain the exact ordered
two-merge chain, with both merge trees equal to trusted application of those two
patches. This proof does not run hooks, a build backend, or candidate Python.

Actual start requires a host-watch sample no older than 120 seconds, capacity
for one job, 14 GiB available memory for the 6 GiB container caps plus 8 GiB
reserve, at least 4 GiB disk reserve, and fresh pinned owner usage below 50%.
A CPU-only thread checks expiry, usage, host-watch freshness, actual memory,
and disk every two seconds. Docker commands and the candidate wait loop check
its status. A failure blocks new gate effects while preserving trusted cleanup,
forces failure, and prevents signing. The normal full-gate receipt schema stays
unchanged; the supervisor result also reports its consumption file and digest.

The frozen 3c77 environment revision passed 39 checks on Wonko in 3.41 seconds,
with 87.43 MB peak memory under its 256 MiB bound, using uv 0.11.22. That evidence
includes the unchanged project launcher with an empty offline cache. It applies
only to exact 3c77 source. The one-use policy and focused validation revisions
are source WIP; their new tests have not yet run under admitted resources.
The previous c9c independent source review and 37 checks remain exact-source
evidence. No isolated Docker qualification or automatic delivery is claimed.

## Fixed focused validation before freeze

The two approved assignments have distinct fixed unit and long profiles. The
common prefix is `uv run --extra test python -m pytest -q`.

| Profile | Fixed remaining arguments |
| --- | --- |
| `petri-client-unit-v1` | `tests/test_petri_client.py` |
| `petri-client-long-v1` | `tests/test_petri_workers.py tests/test_manual_cord.py` |
| `session-scan-unit-v1` | `tests/test_session_failure_scan.py -k 'reassembles or prefilter or concatenated or keeps_complete or quoted'` |
| `session-scan-long-v1` | `tests/test_session_failure_scan.py -k 'not (reassembles or prefilter or concatenated or keeps_complete or quoted)'` |

The owner policy pins each task's exact profile pair. These profiles were
independently reviewed against the approved patches. The client long profile
exercises mocked service and Git/Cord workflow integration; it does not claim a
live REST service test. Scanner long selection complements unit selection and
includes real bounded CLI, cancellation, process cleanup, and ripgrep behavior.
The immutable runner image must already contain required executables and
dependencies. A missing prerequisite, empty selection, or skipped test fails
focused validation; no dependency bootstrap or network access is allowed.

Use `--execute-policy --focused-stage focused-{0|1}-{unit|long}` with the same
private permit/trust pins, signed input, signer, and output arguments. The
focused predicate has the full image/source/isolation fields and exact candidate
head/tree/archive/history pins, with no bundle ID or PR number. Its static full
command fields pin the image contract; the root-selected profile determines the
actual focused command. The trusted entrypoint accepts only the full command
and these four exact argv lists, then guarantees the same fixed exec boundary.
Candidate arguments cannot broaden that whitelist.

Each focused stage independently consumes its one-use intent before preparation.
Its signed integration input binds the exact task/assignment/worker, amended
brief, workflow tuple, head/tree/archive/history, conductor intent, stage, and
submission time. The host verifies actual private brief and patch bytes and
reconstructs the worker tree before starting any container. Trusted runner and
candidate paths remain separate. Linked worktrees resolve their actual common
Git object directory; untrusted alternates are rejected, and reconstructed
objects are written only to bounded task-owned temporary storage.

Focused execution uses the same PostgreSQL, two network namespaces, default-deny
firewalls, trusted probes, mounts, resource caps, logs, exit observation, and
cleanup verifiers as the full gate. It signs a separate
`skybuild.focused-validation.v1` envelope with the owner-configured validation
principal/key. The receipt binds all source/task/command/stage/permit and intent
digests, actual isolation evidence, externally observed exit, cleanup, retained
raw-log SHA-256, and bounded stdout-derived pytest `reported_counts`. These
reported counts have meaning only for the exact owner-approved known patch/tree
and fixed test profiles. Candidate stdout cannot independently prove counts
against arbitrary hostile Python code; such code could forge a final summary.
This focused mode does not make that broader integrity claim.
PASS requires exit zero, a report of at least one passing test, and zero reported failed,
errored, skipped, xfailed, or xpassed tests. Nonempty deselection is reported.
An observed pytest failure can have a signed failure receipt; supervisor or
cleanup uncertainty cannot produce a passing result. CLI success requires a
passing focused verdict as well as the ordinary execution conditions.

The trusted integration producer verifies each receipt before posting the exact
unit or long task stage. The later signed frozen input includes both actual
receipt paths and raw-byte hashes for each task. The combined gate independently
verifies those signatures, approved patch/tree/test bindings, task/workflow tuples,
nonempty passing reported counts, and
immutable prior stage intents before its one full default suite. Focused results
are not full-gate attestations and cannot satisfy the publisher's combined-gate
contract.
