# Optional pilot TLS client and native server support

Task: SKYBUILD-PILOT-TLS-CLIENT. Source base: `257216c5498b95d5a6e56bca60c31abcd6736226`. Owned branch: `task/pilot-tls-client`.

This source-only change adds optional installation trust to the shared Client. `ca_file` creates a fresh `SSLContext(PROTOCOL_TLS_CLIENT)` and loads only the supplied PEM certificates. Hostname verification and `CERT_REQUIRED` stay enabled. Explicit installation trust disables ambient proxy and certificate environment settings. Redirects remain disabled; an installation CA cannot be used with an HTTP URL. Omitted `ca_file` preserves existing system trust behavior. A CA file must be a nonempty regular file no larger than 1 MiB; symlinks are refused and invalid or unreadable PEM material fails before network requests.

The preflight, manual Cord and manual dispatch commands accept `--ca-file PATH`. The general CLI accepts it before the command or within its API-reading/Cord/scheduling subcommands. Existing private `*.ts.net` root, Tailscale-only DNS, exact identity and scoped-grant checks remain in the pilot paths; explicit HTTPS ports such as 8443 continue to be supported by their existing URL validation.

Dispatch stores `ca_sha256` beside the endpoint in its durable intent when installation trust is selected. A retry cannot add, remove or change that trust material, even after a successful send. The Client rereads the CA and verifies the expected digest before constructing its transport, closing the fingerprint-to-context replacement gap. Existing intents without a CA field continue to work without an explicit CA. A changed installation CA requires manual reconciliation/new assignment identity; old intent bytes are never silently rewritten. No database schema change is needed.

Native `skybuild serve` accepts optional paired `--ssl-certfile PATH --ssl-keyfile PATH` and forwards those exact options to Uvicorn. Supplying only one fails during argument validation before database configuration. The existing plain HTTP default and host choices are unchanged. Container publication, certificate generation/distribution, operator state and deployment policy belong to the separate deployment task; this branch does not generate operator keys, call a live endpoint, change network policy or deploy a service.

Focused check:

```sh
rtk proxy env PYTHONPATH=/home/kevin/my_code/skybuild-pilot-tls-client/src nice -n 10 /home/kevin/my_code/skybuild/.venv/bin/python -m pytest tests/test_client_tls.py tests/test_manual_dispatch.py tests/test_client.py tests/test_fleet_preflight.py tests/test_manual_cord.py tests/test_cord_wait.py -q
```

Result: **91 passed**. The TLS tests generate ephemeral test-only certificates in pytest scratch storage using the existing OpenSSL binary and perform bounded loopback TLS handshakes. Correct CA/hostname succeeds despite hostile proxy/CA environment settings. Wrong CA, wrong hostname, expired leaf and unchanged system trust all reject the handshake before any HTTP bearer header reaches the server. Missing/invalid/changed CA material fails before transport. Additional checks cover the isolated trust store, redirects, CA-bound immutable dispatch retries and legacy compatibility, CLI propagation, private preflight scopes, and paired native server arguments. `git diff --check` passes. Parent owns independent exact-head review, combined gate and any separately authorized deployment.
