# ADR 0023: Initial private access through Tailscale

Date: 2026-10-08. Status: accepted owner direction for initial network and member-device reachability; exposure mechanics proposed. Implementation: not started.

## Context and decision

The owner specified Tailscale as part of the initial design, with access from any Tailscale box in the deployment’s group. Use Tailscale initially and make alternative VPN choice configurable later. Interpret the intended group as the deployment’s enrolled tailnet devices; external device-sharing is not an automatic expansion of that set.

Every intended member box can reach the control website and API. Network membership does not automatically grant application project/operation privileges or model-spending authority. Keep the dedicated PostgreSQL database local to the laptop controller.

## Proposed mechanics and consequences

Use a loopback application behind Tailscale Serve HTTPS, one website/API origin, ordinary URLs and the existing proposed application authentication boundary. No public/Funnel exposure or new VPN-management framework is needed. Verify grants/access rules and host firewall for the intended sources and SkyBuild HTTPS endpoint without broadening unrelated network access. Keep enrollment/node credentials outside Git and task evidence.

Serve follows tailnet access rules; details and primary sources are in the [access research](../design/research/tailscale_access_20261008.md). Restrictive policies can prevent the requested reachability and must be reconciled during implementation. Tailscale does not make a sleeping laptop available or change the accepted outage behavior.

## Alternatives and validation

Public hosting and initial VPN-provider configurability were not selected. Alternative private-network configuration is recorded as SKYBUILD-NETWORK-PROVIDERS in the [deferred ledger](../design/deferred.md).

Validate browser/API access from another intended member box, application authorization rejection despite network membership, safe listener/database boundaries, restart/offline behavior and distinct network versus application errors. Actual client setup, policy, DNS/certificates and firewall remain unverified; this record authorizes no configuration change.

Architecture: sections 3 and 5. Implementation implications: area 2 private service exposure and acceptance; initial laptop hosting is unchanged.
