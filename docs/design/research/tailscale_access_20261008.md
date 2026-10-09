# Initial Tailscale access research

Date: 2026-10-08. Public documentation only; no installed client, account, node credentials, live access policy or network connectivity was inspected. [Architecture](../architecture.md) governs.

## Verified public constraints

Tailscale Serve exposes a local service to tailnet devices, supports proxying a loopback HTTP service, requires tailnet HTTPS certificates for HTTPS, and obeys access-control rules. Its documentation recommends a localhost listener when trusting identity headers so clients cannot bypass the proxy. Serve is distinct from public Funnel exposure. [Tailscale Serve documentation](https://tailscale.com/docs/features/tailscale-serve).

Tailscale documents a permissive default tailnet access policy, but deployments can restrict it. Intended enrolled members and devices shared from an external tailnet are different access categories. The actual deployment policy must be checked before assuming any-device reachability. [Official grant examples](https://tailscale.com/docs/reference/examples/grants).

## Design implications and unverified setup

The owner requires initial Tailscale access from every intended enrolled box in the deployment’s tailnet/group. Proposed mechanics are a loopback website/API behind Serve HTTPS, application token/project scopes, and local PostgreSQL. Grant access to the SkyBuild HTTPS endpoint without broadening unrelated ports. Tailscale membership supplies reachability, not application administration. Do not use network identity headers as an implicit replacement for the application authorization contract.

Concrete enrollment, grants, firewall, DNS/certificate setup and restart behavior require later implementation qualification. A second member-device browser/API check and negative application-authorization check belong in bootstrap acceptance. A sleeping laptop stays unavailable. Alternative VPN configuration is explicitly deferred, and no paid infrastructure or always-on control host is implied.
