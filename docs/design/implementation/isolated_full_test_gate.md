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

## Candidate boundary

The supervisor verifies a clean trusted checkout and candidate checkout, the
candidate commit and tree, the target-base ancestry, and the raw
`git archive --format=tar` SHA-256. It rejects unsafe archive members and
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
archive to scratch, verifies the import path, checks connectivity denial, and
starts the unchanged full test command.

## Network isolation

The runner creates one internal IPv4 Docker network without masquerading. A
pinned firewall image runs as a short-lived `NET_ADMIN` sidecar in each
container network namespace. The candidate and PostgreSQL containers retain
`CAP_DROP=ALL`; no host firewall rule changes.

Both namespace policies set IPv4 and IPv6 INPUT, OUTPUT, and FORWARD defaults
to DROP. Candidate IPv4 OUTPUT allows only the exact PostgreSQL address on TCP
5432 plus established replies. PostgreSQL IPv4 INPUT allows only the exact
candidate address on TCP 5432 plus established replies. Both namespaces allow
loopback and established traffic. DNS requests to Docker's embedded resolver
are denied. The runner compares complete `iptables-save` and `ip6tables-save`
output to the policy before release.

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
the candidate exit and cleanup results. Uncertain cleanup remains an explicit
failure.

The helper imports `scripts/trusted_gate_attestation.py`; its raw SHA-256 is
bound by the expected predicate, clean trusted checkout, plan, and GO record.
The key must be an owned regular file with mode 0600 and 32–4096 bytes. The key
is never mounted into a container.

## Execution status

Unit tests cover policy binding, environment and mount rejection, archive
safety, firewall rule parsing, key checks, cleanup reconciliation, and log
redaction. They do not prove Docker behavior. Do not claim a full-suite pass
until an independent source review approves the exact runner commit and root
issues GO for the reviewed plan and resource host. The actual isolated runtime
rehearsal is still required for qualification.
