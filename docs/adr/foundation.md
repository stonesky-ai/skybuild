# Product and planning decisions

Each section retains its original ADR ID. Status applies to the decision, not implementation. [Architecture](../design/architecture.md) governs current requirements.

<a id="adr-0001"></a>
## ADR 0001: SkyBuild product and database boundary

**Accepted, 2026-10-08.** Extract all build tooling, generic skills, tests and operational entry points from SkyKeep into SkyBuild. Keep Cord and Keeper names. Product repositories retain application code/tests and pinned, versioned project adapters. Give SkyBuild its own PostgreSQL database, credentials and migrations; a separate schema in SkyKeep's database is insufficient. Physical hosting remains open.

Inventory every writer, launcher and installed artifact, not only Python modules. Retire temporary wrappers explicitly. Review moves, SQL conversion and launcher adoption separately. SkyKeep becomes a client project. Daily dumps and later restore work follow [ADR 0027](hosting-recovery.md#adr-0027); neither changes database separation. Architecture sections 2 and 7.

<a id="adr-0003"></a>
## ADR 0003: Architecture governs planning

**Accepted, 2026-10-08.** Keep `docs/design/architecture.md` authoritative and `implementation_plan.md` derived. Reconcile design changes, plan implications and major ADRs in the same revision. Use one high-level parent sequence; split an area only when detail or ownership warrants it. Keep task links and open choices current.

Do not treat old plans or the task queue as design authority. Surface conflicts before acting. ADR acceptance records design, not implementation completion or deployment permission. Architecture section 1.
