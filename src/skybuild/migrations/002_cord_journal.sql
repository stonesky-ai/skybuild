CREATE TABLE cord_journal (
    event_id uuid PRIMARY KEY,
    project_id text NOT NULL,
    message_id uuid NOT NULL,
    actor text NOT NULL REFERENCES principals(principal_id),
    action text NOT NULL CHECK (action IN ('send', 'receipt', 'handle', 'reply')),
    original_message_id uuid,
    reply_message_id uuid,
    before_state jsonb,
    after_state jsonb NOT NULL,
    reply_state jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (project_id, message_id) REFERENCES messages(project_id, message_id),
    FOREIGN KEY (project_id, original_message_id) REFERENCES messages(project_id, message_id),
    FOREIGN KEY (project_id, reply_message_id) REFERENCES messages(project_id, message_id),
    CHECK ((action = 'reply') = (reply_message_id IS NOT NULL)),
    CHECK ((action = 'reply') = (reply_state IS NOT NULL))
);
CREATE INDEX cord_journal_message ON cord_journal (project_id, message_id, created_at, event_id);
CREATE FUNCTION refuse_cord_journal_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Cord journal is append-only';
END;
$$;
CREATE TRIGGER cord_journal_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON cord_journal
    FOR EACH STATEMENT EXECUTE FUNCTION refuse_cord_journal_mutation();
