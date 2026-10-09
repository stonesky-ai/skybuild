# Dependency readiness and durable invalidation

This slice follows architecture A33 sections 4–5 and the task workflow's immutable history contract. Markdown remains the live SkyBuild task authority. It adds no worker admission, service launch or authority switch.

Readiness evaluation runs synchronously under the existing project graph lock. A separate PostgreSQL readiness record retains input and assessed generations without changing frozen importer task snapshots. Every accepted task mutation invalidates its transitive dependents in the same transaction, journals the invalidation, and revokes stale readiness. Deferred and superseded dispositions remain intact; previously started work remains held for effect reconciliation.

A guarded `ready` action requires explicit acceptance, no started history, and current completed prerequisites. It acknowledges only the generation evaluated while holding the graph lock. A later input change leaves a newer dirty generation. Manual readiness remains separate from execution authority.

Implementation and independent validation are pending.
