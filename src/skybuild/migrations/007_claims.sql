CREATE TABLE task_claims (
    project_id text NOT NULL,
    task_id text NOT NULL,
    fence bigint NOT NULL CHECK (fence > 0),
    holder text NOT NULL REFERENCES principals(principal_id),
    task_revision bigint NOT NULL CHECK (task_revision > 0),
    lease_until timestamptz NOT NULL,
    held boolean NOT NULL DEFAULT true,
    PRIMARY KEY (project_id, task_id),
    FOREIGN KEY (project_id, task_id) REFERENCES tasks(project_id, task_id)
);
CREATE TABLE claim_journal (
    event_id uuid PRIMARY KEY,
    project_id text NOT NULL,
    task_id text NOT NULL,
    actor text NOT NULL REFERENCES principals(principal_id),
    action text NOT NULL CHECK (action IN ('claim', 'renew', 'release', 'reconcile')),
    reason text NOT NULL,
    before_state jsonb,
    after_state jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (project_id, task_id) REFERENCES tasks(project_id, task_id)
);
CREATE TRIGGER claim_journal_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON claim_journal
    FOR EACH STATEMENT EXECUTE FUNCTION refuse_journal_mutation();
