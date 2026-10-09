# ADR 0024: Separate owner/admin and worker credentials

Date: 2026-10-08. Status: accepted owner direction for owner/admin access and separate project-scoped worker tokens; exact credential/session mechanics proposed. Implementation: not started.

## Context and decision

The owner confirmed the recommended initial application access split: owner/admin credentials plus separate project-scoped worker tokens. This complements Tailscale reachability in [ADR 0023](0023-initial-tailscale-access.md); network membership is not application authorization.

Owner/admin access manages enrollment, credentials and project permissions, and later approvals/budgets through controlled interfaces. Each worker receives its own credential and only the task/Cord operations required in assigned projects. A worker cannot mint credentials, expand scopes, approve its own spending or raise budgets. No credential bypasses execution admission, expiry or billing restrictions.

## Proposed compact mechanism

Reuse the owner-managed principal/hashed-token registry with explicit project and operation checks. Keep stable worker principals, separately revocable/replaceable credentials and server-derived actor/sender identity. Store token verifiers; keep bearer secrets outside Git, task briefs and outputs. Credential replacement retains principal/history and existing task/usage authority. Revocation rejects future authenticated API calls but does not prove previously launched work stopped.

The exact operation matrix, owner bootstrap/recovery, issuance/rotation and browser-session handling remain to be settled. Do not introduce an SSO service, general policy framework or full user-management console for this first service. Provider subscription credentials and Tailscale node credentials remain distinct from SkyBuild worker credentials.

## Alternatives and validation

A shared owner token for all workers or trusting tailnet membership as administrator access would erase the agreed separation. Validate correct/missing/revoked credentials, wrong-project and administrator-operation denial, claimed actor/sender spoofing, isolated worker revocation and credential replacement without new task/budget authority. Physical-stop validation remains part of later execution controls.

Architecture: sections 3, 5 and 8–9. Implementation implications: area 2 authentication/authorization and later owner-only approval controls. This decision creates no credentials and authorizes no implementation.
