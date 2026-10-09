CREATE TABLE task_effects (
    operation_id text PRIMARY KEY,
    project_id text NOT NULL,
    task_id text NOT NULL,
    attempt_id text NOT NULL,
    task_revision bigint NOT NULL CHECK (task_revision > 0),
    authority_epoch bigint NOT NULL CHECK (authority_epoch > 0),
    authority_generation bigint NOT NULL CHECK (authority_generation > 0),
    input_digest text NOT NULL CHECK (input_digest ~ '^[0-9a-f]{64}$'),
    policy_digest text NOT NULL CHECK (policy_digest ~ '^[0-9a-f]{64}$'),
    allocation_refs text[] NOT NULL,
    intent_hash text NOT NULL,
    state text NOT NULL DEFAULT 'intent' CHECK (state IN ('intent', 'unknown', 'cancelled')),
    exposure_held boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (project_id, task_id) REFERENCES tasks(project_id, task_id),
    CHECK (exposure_held = (state <> 'cancelled'))
);
CREATE INDEX task_effects_task ON task_effects (project_id, task_id);
CREATE TABLE effect_journal (
    event_id uuid PRIMARY KEY,
    operation_id text NOT NULL REFERENCES task_effects(operation_id),
    actor text NOT NULL REFERENCES principals(principal_id),
    action text NOT NULL CHECK (action IN ('intent', 'unknown', 'cancelled')),
    reason text NOT NULL,
    before_state jsonb,
    after_state jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER effect_journal_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON effect_journal
    FOR EACH STATEMENT EXECUTE FUNCTION refuse_journal_mutation();
CREATE FUNCTION guard_effect_record() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP <> 'UPDATE' THEN
        RAISE EXCEPTION 'Effect records cannot be deleted or truncated';
    END IF;
    IF (to_jsonb(OLD) - 'state' - 'exposure_held') IS DISTINCT FROM
       (to_jsonb(NEW) - 'state' - 'exposure_held') OR
       OLD.state <> 'intent' OR NEW.state NOT IN ('unknown', 'cancelled') THEN
        RAISE EXCEPTION 'Effect identity and terminal observations are immutable';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER effect_record_update BEFORE UPDATE OR DELETE ON task_effects
    FOR EACH ROW EXECUTE FUNCTION guard_effect_record();
CREATE TRIGGER effect_record_truncate BEFORE TRUNCATE ON task_effects
    FOR EACH STATEMENT EXECUTE FUNCTION guard_effect_record();
