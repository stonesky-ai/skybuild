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

Existing ledger tests require two frozen source commits:
`6d96075f88493d0b54577a2a8c9526f19a78a5ed` and
`d79d2e1947d2c8e9edb577ab5f5093edfa3c94e3`. The supervisor constructs a shallow
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
