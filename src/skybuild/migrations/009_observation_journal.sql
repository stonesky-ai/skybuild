-- Evidence only. These tables confer no execution or completion authority.
CREATE TABLE observation_events (
    project_id text NOT NULL,
    event_id text NOT NULL,
    task_id text NOT NULL,
    attempt_id text NOT NULL REFERENCES cpu_reservations(attempt_id),
    actor text NOT NULL REFERENCES principals(principal_id),
    identity_hash text NOT NULL,
    identity jsonb NOT NULL,
    source_sequence bigint NOT NULL CHECK (source_sequence > 0),
    observed_at timestamptz NOT NULL,
    received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    state text NOT NULL CHECK (state IN ('running', 'waiting', 'unknown', 'exited', 'interrupted', 'result-reported')),
    evidence_refs jsonb NOT NULL,
    payload_hash text NOT NULL,
    PRIMARY KEY (project_id, event_id),
    UNIQUE (project_id, identity_hash, source_sequence),
    FOREIGN KEY (project_id, task_id) REFERENCES tasks(project_id, task_id)
);
CREATE TRIGGER observation_events_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON observation_events
    FOR EACH STATEMENT EXECUTE FUNCTION refuse_journal_mutation();
CREATE TABLE observation_projections (
    project_id text NOT NULL,
    task_id text NOT NULL,
    component_id text NOT NULL,
    source_id text NOT NULL,
    identity_hash text NOT NULL,
    source_sequence bigint NOT NULL CHECK (source_sequence > 0),
    event_id text NOT NULL,
    PRIMARY KEY (project_id, task_id, component_id, source_id),
    FOREIGN KEY (project_id, event_id) REFERENCES observation_events(project_id, event_id)
);
