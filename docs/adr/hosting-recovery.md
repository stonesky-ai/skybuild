# Hosting, network and recovery decisions

Each section retains its original ADR ID. Status applies to the decision, not implementation. [Architecture](../design/architecture.md) governs current requirements.

<a id="adr-0006"></a>
## ADR 0006: Initial laptop controller

**Accepted, 2026-10-08.** Run initial SkyBuild API and dedicated PostgreSQL database on the owner's laptop. Keep database separate from SkyKeep and disposable tests. Preserve interactive laptop resources; hosting the controller does not imply heavy worker or model serving. Discuss measured constraints before moving or provisioning another host; do not automatically rent, upgrade or reimage.

Persist task/message data across normal restart. Report API unavailability during sleep/offline periods; never silently write another store. Contact-loss policy follows [ADR 0007](execution.md#adr-0007), initial network follows [ADR 0023](#adr-0023), recovery follows [ADR 0027](#adr-0027). Architecture sections 3, 12 and 15.

<a id="adr-0020"></a>
## ADR 0020: Deferred cost-aware job transfer

**Accepted later capability; implementation deferred, 2026-10-08.** Preserve portable, versioned checkpoints, pinned inputs and durable job/artifact identity now. Later, consider moving a suitable tail job from a paid host to an available laptop when avoidable rental cost exceeds transfer, setup and delay. Expose owner availability override. Example machine prices are not selected rentals.

Transfer requires qualified destination resources/dependencies, verified state copy, source fencing and one destination owner. Lost acknowledgment cannot permit duplicate execution. Stop a paid host only after destination readiness, no remaining owned work/resources and lifecycle authorization. Do not assume live process-memory migration, unlimited rental or silent model changes. Automatic thresholds remain open. Architecture sections 9 and 15.

<a id="adr-0021"></a>
## ADR 0021: Deferred calendars, bursts and economics spike

**Accepted later capabilities and research/test spike; backend and economics open, 2026-10-08.** Defer recurring/one-off capacity calendars, owner reservations, bounded cloud workload bursts and comparison of configured boxes with serverless/managed batch. Preserve job/result/checkpoint and host-availability seams. No backend is selected or presumed cheaper.

Later forecasts must include actual reachability, timezone, dependencies, serialized integration, memory/test lanes and shared inference limits. Do not rent CPU merely to wait for model output. Pin burst scope/resources to existing money/runtime authority; new backlog cannot extend it. Keep controller/PostgreSQL authority on its designated host and confirm worker shutdown. The spike compares representative CPU-ready work with local and qualified remote baselines under an agreed cap, including startup, idle, transfer, retry and stop costs; record estimates separately from observed charges. No cloud start or spend is authorized here. Architecture sections 9 and 15.

<a id="adr-0023"></a>
## ADR 0023: Initial Tailscale access

**Accepted network choice and intended member reachability; exposure mechanics proposed, 2026-10-08.** Make the website/API reachable from intended enrolled boxes in the deployment tailnet/group. Tailscale membership is network reachability, not project, operation or spending authority. Keep PostgreSQL local to the laptop. Defer alternative VPN configuration.

Proposed: loopback app behind Tailscale Serve HTTPS, one website/API origin, no public Funnel. Verify grants, firewall, browser/API access and application auth denial from member boxes. Keep node credentials out of Git. Tailscale does not prevent laptop outages. Architecture sections 3 and 5.

<a id="adr-0025"></a>
## ADR 0025: Historical backup deferral

**Superseded by [ADR 0027](#adr-0027), 2026-10-08.** This decision once deferred encrypted off-laptop and pre-migration backups and removed them as bootstrap/cutover prerequisites. It grants no current exemption from the later daily-dump decision. GitHub source pushes alone never preserve unexported PostgreSQL data. Original record remains in Git history.

<a id="adr-0027"></a>
## ADR 0027: Daily dumps and journal recovery

**Accepted daily dump/commit and low-priority restore/rebuild; publication/journal mechanics open, 2026-10-08.** Schedule a bounded CPU task to dump SkyBuild's dedicated PostgreSQL database to a file committed daily and pushed through normal GitHub delivery. Distinguish local commit from confirmed remote preservation. Reopen restore and journal-based reconstruction as a separate low-priority task; neither is a launch-free service prerequisite. Journal location and commit interval remain undecided.

Proposed dump job: prevent overlap, publish complete files with identity/version/hash, isolate backup worktree, and preserve prior good artifacts through offline or failed dump/commit/push. Select format, destination, schedule, retention, size, encryption and key custody before enabling publication. Keep secrets outside Git. Journal artifacts need stable event/effect IDs, hashes and a validated boundary relative to the dump. Test disposable restore/rebuild and corrupt/missing coverage. Rebuild cannot replay production effects, clear stops, renew approval or reset usage. `pg_dump` covers one database, not cluster roles/tablespaces; dump plus journal is not WAL point-in-time recovery. Architecture sections 3–4, 7–9 and 16.
