# SkyBuild architecture decisions

[Architecture](../design/architecture.md) governs; [implementation plan](../design/implementation_plan.md) follows it. These eight topic files contain 34 decisions. Load only the topic needed for a task. Each section retains its original ADR ID and states whether the choice is accepted, proposed or superseded. Acceptance does not mean implementation or deployment approval.

| Topic | ADR IDs |
| --- | --- |
| [Product and planning](foundation.md) | 0001, 0003 |
| [Task authority and workflow](tasks.md) | 0002, 0029, 0031 |
| [Execution control](execution.md) | 0004, 0007, 0016, 0018, 0019, 0024 |
| [Inference capacity](inference-capacity.md) | 0008–0012, 0014, 0017, 0032 |
| [Inference execution](inference-runtime.md) | 0005, 0013, 0015, 0022 |
| [Hosting, network and recovery](hosting-recovery.md) | 0006, 0020, 0021, 0023, 0025, 0027 |
| [Git and integration](integration.md) | 0026, 0028, 0030 |
| [Review and quality](quality.md) | 0033, 0034 |

Use a decision's `#adr-NNNN` anchor for links. The original individual files remain in Git history at commit `046163561ac6ff00c51ee6300fc3df66100a95d4`; they are not active policy. Do not import them for routine checks. If a decision changes, update architecture and its derived plan in the same revision, state the new status here, and retain the old rationale in Git history.
