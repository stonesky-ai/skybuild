# Launch-free observation journal scaffold

This task derives from architecture A34 sections 8–10 and implementation plan section 5 at base commit 602778093613adf7eeb0cc38328236f2f169d095. It implements Store-only evidence receipt and a fake observer fixture. Markdown task authority remains unchanged. No live database, worker, service, process launcher or deployment is part of this task.

The journal preserves immutable observation identity and source order. A projection may advance only for the pinned identity and a strictly newer source sequence. Identical replay is harmless; conflicting replay is rejected; reordered evidence remains historical. Observations never accept completion, release ownership or reservations, settle effects, renew authorization, or infer death from missing heartbeat.

A durable offline host spool and physical observer qualification remain later work. This scaffold does not establish offline host guarantees.

Implementation phase: in progress. Next action: implement journal migration, authenticated Store methods, fake receipt/replay tests and bounded TLA+ model. Responsible: observation journal task author. Independent review and integration remain pending.
