-- Durable projection bookkeeping stays separate from frozen import snapshots.
CREATE TABLE task_readiness (
    project_id text NOT NULL,
    task_id text NOT NULL,
    input_generation bigint NOT NULL DEFAULT 1 CHECK (input_generation > 0),
    assessed_generation bigint NOT NULL DEFAULT 0 CHECK (assessed_generation >= 0 AND assessed_generation <= input_generation),
    PRIMARY KEY (project_id, task_id),
    FOREIGN KEY (project_id, task_id) REFERENCES tasks(project_id, task_id)
);
INSERT INTO task_readiness (project_id, task_id) SELECT project_id, task_id FROM tasks;
