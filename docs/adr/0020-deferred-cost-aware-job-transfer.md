# ADR 0020: Deferred cost-aware placement and portable job transfer

Date: 2026-10-08. Status: accepted owner direction for a later advanced capability; implementation explicitly deferred. Mechanics and numeric economics remain open. Implementation: not started.

## Context and decision

The owner would move the last job off a rented machine to a suitable available laptop to avoid unnecessary rental costs. SkyBuild should anticipate that when splitting and assigning work, and expose an owner override when the laptop will be unavailable on a particular day.

The owner's $8/hour, 32-core, 64-GB machine is an illustrative example, not a selected rental. Preserve portable/versioned checkpoints, pinned inputs and durable artifact/job identity in the initial contracts. Defer the cost-aware scheduler, transfer automation and availability/override interface until core execution and checkpoint recovery work.

## Future mechanics and consequences

Prefer cooperative application checkpoints or bounded resumable subtasks. Arbitrary transfer of live process memory is not assumed. Destination qualification includes resources, architecture/OS/dependencies, input/artifact access and required GPU/model profile. Fewer cores may increase completion time; owner use and scheduled offline periods constrain laptop suitability.

Compare expected avoidable rental cost with transfer/setup, rework and completion delay, retaining uncertainty. Use CPU-observed job/host state rather than model polling. Exact automatic-versus-recommended transfer policy and numeric thresholds belong to the later area plan.

Quiesce/checkpoint consistently, durably copy/verify state, reconcile/fence source execution and establish one destination owner before resuming. Retain operation identity and uncertain effects. Lost acknowledgment cannot justify duplicate execution. Stop the paid host only after accepted checkpoint/destination readiness, no remaining owned work/resources and authorization under its lifecycle policy.

An owner override can keep work remote or defer it when the laptop is offline, within existing monetary/runtime limits. Transfer failure does not disable hard shutdown backstops, grant unlimited rental or silently select a different model. This decision authorizes no machine start, rent, transfer or shutdown during planning.

## Alternatives

Leaving one small job on an otherwise idle expensive machine wastes rental capacity. Migrating immediately without portability, suitability or fencing can lose or duplicate work. Building this optimizer before the basic reporting/checkpoint system would add unnecessary initial scope.

Architecture: sections 9 and 15. Implementation: preserve the portability seam now; later work is [SKYBUILD-COST-AWARE-JOB-TRANSFER](../design/deferred.md#skybuild-cost-aware-job-transfer--move-portable-work-off-paid-hosts). This extends the independent CPU-job lifecycle in [ADR 0019](0019-expiry-handoffs-and-model-cutoff.md) without making transfer a prerequisite.
