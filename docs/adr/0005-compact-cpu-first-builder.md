# ADR 0005: Compact CPU-first builder and qualified inference

Date: 2026-10-08. Status: owner goals confirmed; component/routing/lifecycle mechanics proposed. Implementation: not started.

## Context

The owner wants capable compact components, accepts somewhat denser code to save tokens, has plentiful CPU and limited Codex/Claude/Grok tokens, and reports a model at LLM.brodson.net. A rented RunPod GPU may help a sufficiently large qualified workload. SkyBuild must build itself before building SkyKeep efficiently.

## Proposed decision

Start with one API/PostgreSQL service and small shared modules/client. Later use Keeper plus an independent observer per worker box. Reuse deterministic tools for discovery, validation, integration, observation and recovery. Compress repetition and context while keeping ownership, auth and transaction boundaries explicit.

Route bounded tasks by demonstrated capability: CPU first, qualified brodson when suitable, qualified RunPod for an approved ready backlog, and premium reasoning for difficult/high-consequence decisions. Begin with inference-to-patch/structured-output, not a new general agent framework. Account for shared test endpoint capacity, model/context reloads, correction/review effort and useful throughput. Do not silently retry or escalate paid calls.

Separate host policies: burst CPU/GPU workers drain and stop on explicit budget/deadline/ready-backlog rules; the durable task/mailbox controller remains available until planned handover/offline maintenance. Provider lifecycle and residual resources require reconciliation. A stable accepted controller/manual recovery path stays outside candidate self-builds; a candidate cannot promote itself.

## Alternatives and consequences

Premium model roles for polling/integration or an always-warm rented model without ready work waste scarce capacity. A broad orchestration/plugin stack increases code and repeated context before there is a need. Excessively cryptic safety logic saves few tokens while increasing correction cost. A Spot/control-server lifecycle shared with transient workers undermines task and stop authority.

Model/GPU selection, measured qualification and numeric budgets remain open. Initial laptop placement is now settled by [ADR 0006](0006-laptop-first-control-host.md); the earlier dedicated-host proposal applies only to a later owner-approved move. Historical endpoint notes show contention risks but do not identify today's model. Keep all code and infrastructure activity separately authorized. Architecture sections 13–17 govern the details; implementation planning stays high level.
