-- Staged workflow enrollment uses the existing task record. Historical journal
-- events remain immutable. Explicit enrollment maps ambiguous records to Hold;
-- final default enrollment and the captured-snapshot rehearsal belong to rollout.
ALTER TABLE tasks ADD CONSTRAINT tasks_petri_workflow_shape CHECK (
    metadata->'_skybuild_workflow'->'petri' IS NULL OR (
        jsonb_typeof(metadata->'_skybuild_workflow'->'petri') = 'object'
        AND metadata->'_skybuild_workflow'->'petri'->>'schema_version' = '1'
        AND jsonb_typeof(metadata->'_skybuild_workflow'->'petri'->'token') = 'object'
        AND metadata->'_skybuild_workflow'->'petri'->'token'->>'place'
            IN ('ready', 'working', 'validating', 'integrating', 'done', 'deferred', 'hold')
    ) IS TRUE
);

-- Ownership captures the attempt input revision. Dispatch independently pins the
-- current task revision, which can advance through workflow progress.
ALTER TABLE cpu_reservations ADD COLUMN claim_task_revision bigint;
ALTER TABLE cpu_reservations ADD CHECK (claim_task_revision IS NULL OR claim_task_revision > 0);
-- Null is the legacy encoding: its claim revision equals task_revision. Never
-- rewrite existing reservation records or bypass their immutable triggers.
