# ADR 0021: Deferred capacity calendars, workload bursts and an economics spike

Date: 2026-10-08. Status: accepted owner direction for later capacity planning and a research-and-test spike; implementation deferred. Compute backend, numeric economics and automation policy are unselected. Implementation: not started.

## Context and decision

The owner has transient capacity, such as a shared laptop unavailable Thursdays. Ready integration, bundling/merging or validation jobs could accumulate into a batch, occupy a rented machine for a bounded period and then release it. The control panel should let the owner judge whether spending money achieves a desired completion time.

Support recurring and one-off availability, owner reservations/overrides and task/checkpoint forecasts. Compare local/transient execution with a selected cloud workload's estimated finish time, total cost and uncertainty. Stage ready work, use appropriate parallel capacity, drain/preserve results and verify shutdown when that workload ends. The hour-or-two and 32-minute examples are illustrative, not duration or billing promises.

The owner also requests a later spike investigating configured box pools and suitable serverless/managed-batch alternatives, including a bounded test. No backend is selected or assumed cheaper. Preserve the shared portable job/result/checkpoint contract; defer advanced backend implementation until evidence supports it.

## Future mechanics and constraints

Distinguish declared calendars from actual reachability/resources. Forecast against timezone-aware availability and drain/checkpoint before household reservations. Account for dependency release, serialized integration/merge ownership, memory/test lanes and shared endpoint limits. Work waiting on model output is not ready CPU backlog; do not rent a CPU host merely to wait for inference.

A burst binds pinned workload scope, inputs, resource profile and existing money/runtime authority. New backlog cannot silently extend it. Keep controller/PostgreSQL authority on its designated host. CPU results are collected without model watching; later model review does not require leaving the burst machine running. Existing model billing/cutoff controls, uncertain-operation reconciliation and independent shutdown backstops still apply.

The spike researches current primary pricing/capability documentation, then compares representative pinned CPU-ready work against a local baseline and at least one qualified remote candidate within an agreed experiment cap. Measure cost per accepted workload and end-to-end completion, including startup/staging, idle/serial work, transfers/storage, retries and confirmed stop/cancellation. Distinguish estimates from observed charges and untested options. Record reproducible evidence and a recommendation before advanced backend rollout. No production mutations or paid inference are needed merely to test CPU economics.

## Alternatives and scope

Assuming all machines are always available misses household constraints. Starting a large machine for inference-blocked or mostly serialized work can add cost without useful speed. Assuming serverless fits or is cheaper without a test is unsupported.

Reuse Store/Observer/Runner/provider adapters and a small deterministic planner rather than a new cluster framework or always-on model scheduler. [SKYBUILD-BURST-CAPACITY-PLANNING](../design/deferred.md#skybuild-burst-capacity-planning--schedule-transient-capacity-and-cloud-batches) and [SKYBUILD-COMPUTE-ECONOMICS-SPIKE](../design/deferred.md#skybuild-compute-economics-spike--measure-box-pools-bursts-and-serverless) are deferred. Initial laptop hosting, reporting and full extraction do not depend on them. [ADR 0020](0020-deferred-cost-aware-job-transfer.md) covers complementary portable tail-job transfer.

Architecture: sections 9 and 15. Implementation implications: preserve capacity/job/availability seams now; later area plans follow the spike. This decision authorizes no cloud start, spend, experiment or live calendar configuration during planning.
