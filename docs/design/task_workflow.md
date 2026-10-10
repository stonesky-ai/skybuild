# SkyBuild task workflow

This document derives from architecture A42, dated 2026-10-09.
[Architecture section 5](architecture.md#5-bootstrap-rest-and-task-contract) governs the workflow.
The project name is **Petri**. Its priority is **5**.
The owner requested design and simplicity analysis before implementation.

The [Petri plan](petri_workflow.md) defines the current proposed workflow.
It replaces the earlier proposal that used many phases.
Use exactly seven places: Ready, Working, Validating, Integrating, Done, Deferred and Hold.
The normal path is Ready, Working, Validating, Integrating, Done.

Unit tests, scans, long tests, code review and needs rebase are validation stages.
The token carries their results. The stages are not additional places.
Confirmed validation and integration failures return the task to Ready with the fault.
Hold and Deferred return to Ready after release and reassessment.
The transition table defines all other permitted movements and guards.

Each task keeps one identity and one current token.
PostgreSQL through the authenticated API remains authoritative.
Commit the state change, journal event and invalidation together.
Check revisions, repeated operation IDs, current evidence, claim fences and unresolved effects.
A UI label cannot bypass these checks.

Keep late results against their original inputs. Do not let late results advance the current task.
Append corrections to history. Do not edit old journal events.
Split and merge preserve identity, lineage, dependency mapping and consumed or uncertain usage.
Supersession records replaced scope. It does not mean accepted completion or another place.

Each unfinished task names its next action, responsible party and blocker.
Dependencies can prevent work while the task remains in Ready.
Deferral triggers use dates with time-zone offsets or stable milestone references.
A missed trigger causes reassessment. It does not renew execution approval.
Hold and deferral requests remain pending while effects are unresolved.
Unknown publication remains Integrating until the actual outcome is known.

The workbench shows seven columns, validation results, freshness, faults and dependencies.
It explains the next-work order.
Basic task management does not require inference.
Code tasks still need independent review.
Dedicated complexity gates and multi-person approval chains keep their existing deferred scope.

The design does not prove that the live service enforces the new workflow.
The Petri plan defines 12 tasks and their acceptance checks.
Existing [reassessment models](models/Reassessment.md), [review policy](review_policy.md) and publication controls remain applicable.
