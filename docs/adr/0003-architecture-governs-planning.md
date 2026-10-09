# ADR 0003: Architecture governs planning

Date: 2026-10-08. Status: accepted from explicit owner direction. Documentation structure: established in this revision; architecture content remains an evolving draft.

## Context

The dated combined plan mixes architecture, research and work sequencing. The owner wants architecture to remain the governing document and implementation detail to grow only where needed.

## Decision

Maintain docs/design/architecture.md and docs/design/implementation_plan.md as the canonical pair. Architecture changes carry their implementation and task implications in the same revision. ADRs explain major choices and are reconciled with the governing architecture. Begin with one high-level implementation plan; split by area only when complexity or ownership makes that useful. Keep parent sequencing and architecture references current.

## Alternatives and consequences

Keeping multiple dated active plans or making the task queue the design authority permits drift. Fully detailed plans for every area now would create speculative work and maintenance cost. Preserve old combined text in Git history and research snapshots rather than competing active plans.

Future agents start at architecture.md, then the derived plan and authoritative task store. Accepted ADRs record decisions, not implementation completion or deployment permission. Surface and reconcile conflicts before acting.

Architecture: section 1. Implementation implications: a compact parent sequence, explicit open choices and stable links from tasks.
