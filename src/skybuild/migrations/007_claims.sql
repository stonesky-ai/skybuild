ALTER TABLE principal_grants DROP CONSTRAINT principal_grants_operation_check;
ALTER TABLE principal_grants ADD CONSTRAINT principal_grants_operation_check
    CHECK (operation IN ('tasks:read', 'tasks:write', 'tasks:claim', 'cord:send', 'cord:read', 'cord:handle'));
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
CREATE FUNCTION guard_claim_record() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP <> 'UPDATE' THEN
        RAISE EXCEPTION 'Claim fence records cannot be deleted or truncated';
    END IF;
    IF NEW.project_id <> OLD.project_id OR NEW.task_id <> OLD.task_id OR
       NOT ((OLD.held AND NEW.fence = OLD.fence AND NEW.holder = OLD.holder AND
             NEW.task_revision = OLD.task_revision) OR
            (NOT OLD.held AND NEW.held AND NEW.fence = OLD.fence + 1)) THEN
        RAISE EXCEPTION 'Claim fences must increase on reacquisition';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER claim_record_update BEFORE UPDATE OR DELETE ON task_claims
    FOR EACH ROW EXECUTE FUNCTION guard_claim_record();
CREATE TRIGGER claim_record_truncate BEFORE TRUNCATE ON task_claims
    FOR EACH STATEMENT EXECUTE FUNCTION guard_claim_record();
