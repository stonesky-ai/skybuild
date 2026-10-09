ALTER TABLE tasks DROP CONSTRAINT tasks_status_check;
ALTER TABLE tasks ADD CONSTRAINT tasks_status_check
    CHECK (status IN ('proposed', 'ready', 'in-progress', 'blocked', 'deferred', 'done', 'superseded'));

CREATE TABLE task_lineage (
    event_id uuid PRIMARY KEY,
    project_id text NOT NULL,
    source_task_id text NOT NULL,
    target_task_id text NOT NULL,
    action text NOT NULL CHECK (action IN ('split', 'merge')),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (project_id, source_task_id) REFERENCES tasks(project_id, task_id),
    FOREIGN KEY (project_id, target_task_id) REFERENCES tasks(project_id, task_id),
    CHECK (source_task_id <> target_task_id),
    UNIQUE (project_id, source_task_id, target_task_id, action)
);
CREATE TRIGGER task_lineage_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON task_lineage
    FOR EACH STATEMENT EXECUTE FUNCTION refuse_journal_mutation();
