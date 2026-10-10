-- Trusted usage history is separate from task metadata, CPU reservations and effects.
ALTER TABLE principal_grants DROP CONSTRAINT principal_grants_operation_check;
ALTER TABLE principal_grants ADD CONSTRAINT principal_grants_operation_check
    CHECK (operation IN (
        'tasks:read', 'tasks:write', 'tasks:claim', 'tasks:usage-record',
        'tasks:usage-resolve', 'integration:attest', 'cord:send', 'cord:read', 'cord:handle'
    ));

CREATE TABLE task_usage_events (
    event_id uuid PRIMARY KEY,
    project_id text NOT NULL,
    task_id text NOT NULL,
    event_kind text NOT NULL CHECK (event_kind IN ('consumed', 'uncertain', 'resolved')),
    attempt_id text NOT NULL CHECK (length(btrim(attempt_id)) BETWEEN 1 AND 200),
    task_journal_event_id uuid NOT NULL REFERENCES task_journal(event_id),
    task_revision bigint NOT NULL CHECK (task_revision > 0),
    definition_revision bigint NOT NULL CHECK (definition_revision > 0),
    input_generation bigint NOT NULL CHECK (input_generation >= 0),
    claim_fence bigint NOT NULL CHECK (claim_fence > 0),
    provider text NOT NULL CHECK (length(btrim(provider)) BETWEEN 1 AND 200),
    model text NOT NULL CHECK (length(btrim(model)) BETWEEN 1 AND 200),
    pool_id text NOT NULL CHECK (length(btrim(pool_id)) BETWEEN 1 AND 200),
    policy_window_id text NOT NULL CHECK (length(btrim(policy_window_id)) BETWEEN 1 AND 200),
    operation_id text NOT NULL CHECK (length(btrim(operation_id)) BETWEEN 1 AND 200),
    unit text NOT NULL CHECK (length(btrim(unit)) BETWEEN 1 AND 64),
    quantity text NOT NULL CHECK (quantity ~ '^(0|[1-9][0-9]{0,17})([.][0-9]{0,11}[1-9])?$'),
    evidence_ref text NOT NULL CHECK (length(btrim(evidence_ref)) BETWEEN 1 AND 1024),
    evidence_sha256 text NOT NULL CHECK (evidence_sha256 ~ '^[0-9a-f]{64}$'),
    reason text NOT NULL CHECK (length(btrim(reason)) BETWEEN 1 AND 4096),
    actor text NOT NULL REFERENCES principals(principal_id),
    resolves_event_id uuid REFERENCES task_usage_events(event_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (project_id, task_id) REFERENCES tasks(project_id, task_id),
    UNIQUE (project_id, provider, operation_id),
    UNIQUE (resolves_event_id),
    CHECK ((event_kind = 'resolved') = (resolves_event_id IS NOT NULL)),
    CHECK (event_kind <> 'uncertain' OR quantity <> '0')
);
CREATE INDEX task_usage_events_task ON task_usage_events
    (project_id, task_id, created_at, event_id);
CREATE INDEX task_usage_events_attempt ON task_usage_events
    (project_id, task_id, attempt_id, provider, model, pool_id, policy_window_id);

CREATE FUNCTION guard_task_usage_event() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, skybuild AS $$
DECLARE origin task_usage_events%ROWTYPE;
        task_event task_journal%ROWTYPE;
        attempt_binding jsonb;
        token jsonb;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'Task usage history is append-only';
    END IF;
    SELECT * INTO task_event FROM task_journal WHERE event_id = NEW.task_journal_event_id;
    IF NOT FOUND OR task_event.project_id <> NEW.project_id OR
       task_event.task_id <> NEW.task_id OR task_event.revision <> NEW.task_revision THEN
        RAISE EXCEPTION 'Usage event must reference the exact immutable task revision';
    END IF;
    attempt_binding := task_event.after_state #> '{metadata,_skybuild_workflow,petri,attempt_binding}';
    token := task_event.after_state #> '{metadata,_skybuild_workflow,petri,token}';
    IF attempt_binding IS NULL OR
       attempt_binding->>'attempt_id' IS DISTINCT FROM NEW.attempt_id OR
       attempt_binding->>'task_revision' IS DISTINCT FROM NEW.task_revision::text OR
       attempt_binding->>'input_generation' IS DISTINCT FROM NEW.input_generation::text OR
       attempt_binding->>'claim_fence' IS DISTINCT FROM NEW.claim_fence::text OR
       token->>'definition_revision' IS DISTINCT FROM NEW.definition_revision::text THEN
        RAISE EXCEPTION 'Usage event must match an immutable attempt binding';
    END IF;
    IF NEW.event_kind = 'resolved' THEN
        SELECT * INTO origin FROM task_usage_events
        WHERE event_id = NEW.resolves_event_id FOR UPDATE;
        IF NOT FOUND OR origin.event_kind <> 'uncertain' THEN
            RAISE EXCEPTION 'Usage resolution requires an uncertain origin event';
        END IF;
        IF origin.project_id <> NEW.project_id OR origin.task_id <> NEW.task_id OR
           origin.task_journal_event_id <> NEW.task_journal_event_id OR
           origin.attempt_id <> NEW.attempt_id OR origin.task_revision <> NEW.task_revision OR
           origin.definition_revision <> NEW.definition_revision OR
           origin.input_generation <> NEW.input_generation OR origin.claim_fence <> NEW.claim_fence OR
           origin.provider <> NEW.provider OR origin.model <> NEW.model OR
           origin.pool_id <> NEW.pool_id OR origin.policy_window_id <> NEW.policy_window_id OR
           origin.unit <> NEW.unit THEN
            RAISE EXCEPTION 'Usage resolution must retain the original task, attempt and budget binding';
        END IF;
        IF origin.actor = NEW.actor THEN
            RAISE EXCEPTION 'A different trusted principal must resolve usage exposure';
        END IF;
        IF EXISTS (SELECT 1 FROM task_usage_events WHERE resolves_event_id = origin.event_id) THEN
            RAISE EXCEPTION 'Uncertain usage already has an immutable resolution';
        END IF;
    ELSIF NEW.resolves_event_id IS NOT NULL THEN
        RAISE EXCEPTION 'Only a resolution event may link to an uncertain event';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER task_usage_event_insert_guard BEFORE INSERT ON task_usage_events
    FOR EACH ROW EXECUTE FUNCTION guard_task_usage_event();
CREATE TRIGGER task_usage_event_update_guard BEFORE UPDATE OR DELETE ON task_usage_events
    FOR EACH ROW EXECUTE FUNCTION guard_task_usage_event();
CREATE TRIGGER task_usage_event_truncate_guard BEFORE TRUNCATE ON task_usage_events
    FOR EACH STATEMENT EXECUTE FUNCTION guard_task_usage_event();
