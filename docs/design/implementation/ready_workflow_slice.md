# Guarded definition readiness

An explicit `ready` task action moves a proposed or blocked task with acceptance criteria to `ready / ready-for-work`. In this early slice it accepts only tasks with no dependencies and no history of started work. The action records a reason, revision and immutable journal event. A later definition edit invalidates readiness and returns the task to blocked reassessment. The next action is fixed to “Await explicit admission and ownership”; callers cannot replace it with an execution instruction.

This is a conservative definition-readiness marker, not a work claim, spend approval or worker start. Tasks with dependencies await durable dependent invalidation and dependency-aware readiness; previously started tasks await effect reconciliation. The imported SkyBuild project still refuses all task writes while Markdown is authoritative. Store and HTTP tests cover guard refusal, idempotent replay, revision/journal updates and invalidation after edit.

Independent `workflow_review` accepted the final guarded transition and corrected refusal test. The full disposable PostgreSQL suite passed 139 tests with the existing Starlette TestClient deprecation warning.
