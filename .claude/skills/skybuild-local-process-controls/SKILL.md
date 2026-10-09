---
name: skybuild-local-process-controls
description: Implement or review SkyBuild local preview start/stop controls shared across checkouts, including Dunsel process identity and crash recovery. Use managed-worker admission and deployment procedures for fleet work.
---

# Local process controls

Multiple checkouts can control the same local preview. A process may outlive its launcher, and a failed state write does not prove that a spawn failed. Preserve the single-process invariant across those boundaries.

- Use one shared lock for every process-identity publication, replacement, and deletion, including writes performed by the worker itself. A launcher-only lock leaves worker publication races open. Check that worker lock acquisition cannot deadlock the parent's startup handshake.
- Persist a blocking launch intent before spawning. If spawning may have succeeded but subsequent identity recording fails, retain that exposure and refuse another launch until physical exit or identity is established. Clear intent on a known no-child failure or confirmed exit; a timeout or failed termination is not confirmation.
- Publish identity through a private pending file and atomic replacement under the same lock. Preserve the last valid identity if writing the replacement fails. Pin kernel start time and exact command arguments, rather than trusting a reused PID.
- Distinguish absent or confirmed stale records from malformed, unreadable, or partially published records. Unknown identity must block a new start rather than silently become “no process.” Keep recovery explicit; do not automatically discard uncertain records to make retry succeed.

For changes to these paths, test observable process counts with deterministic failure injection: a second checkout during publication, a post-spawn state-write failure, replacement-write failure, and termination whose result remains unknown. Use fake processes where possible; ordinary UI source work does not authorize a live worker launch. Record the exact reviewed head and invalidate acceptance after a safety fix.

These rules concern fixed local previews. They do not grant deployment, managed-worker launch, or inference authority.
