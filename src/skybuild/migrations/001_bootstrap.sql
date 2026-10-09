CREATE TABLE principals (
    principal_id text PRIMARY KEY,
    token_verifier text NOT NULL UNIQUE,
    is_admin boolean NOT NULL DEFAULT false
);
CREATE TABLE principal_grants (
    principal_id text NOT NULL REFERENCES principals(principal_id),
    project_id text NOT NULL,
    operation text NOT NULL CHECK (operation IN ('tasks:read', 'tasks:write', 'cord:send', 'cord:read', 'cord:handle')),
    PRIMARY KEY (principal_id, project_id, operation)
);
CREATE TABLE tasks (
    project_id text NOT NULL,
    task_id text NOT NULL,
    title text NOT NULL,
    description text NOT NULL,
    status text NOT NULL CHECK (status IN ('proposed', 'ready', 'in-progress', 'blocked', 'deferred', 'done')),
    priority integer NOT NULL DEFAULT 0,
    acceptance_criteria text[] NOT NULL DEFAULT '{}',
    architecture_refs text[] NOT NULL DEFAULT '{}',
    assignee text,
    phase text NOT NULL,
    next_action text,
    blocker text,
    responsible text NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}',
    revision integer NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, task_id),
    CHECK (status = 'done' OR (length(responsible) > 0 AND (length(next_action) > 0 OR length(blocker) > 0))),
    CHECK (jsonb_typeof(metadata) = 'object')
);
CREATE TABLE task_dependencies (
    project_id text NOT NULL,
    task_id text NOT NULL,
    dependency_id text NOT NULL,
    PRIMARY KEY (project_id, task_id, dependency_id),
    FOREIGN KEY (project_id, task_id) REFERENCES tasks(project_id, task_id),
    FOREIGN KEY (project_id, dependency_id) REFERENCES tasks(project_id, task_id),
    CHECK (task_id <> dependency_id)
);
CREATE TABLE task_journal (
    event_id uuid PRIMARY KEY,
    project_id text NOT NULL,
    task_id text NOT NULL,
    actor text NOT NULL REFERENCES principals(principal_id),
    operation text NOT NULL,
    revision integer NOT NULL,
    reason text NOT NULL,
    before_state jsonb,
    after_state jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (project_id, task_id) REFERENCES tasks(project_id, task_id),
    UNIQUE (project_id, task_id, revision)
);
CREATE FUNCTION refuse_journal_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Task journal is append-only';
END;
$$;
CREATE TRIGGER task_journal_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON task_journal
    FOR EACH STATEMENT EXECUTE FUNCTION refuse_journal_mutation();
CREATE TABLE idempotency (
    principal_id text NOT NULL REFERENCES principals(principal_id),
    project_id text NOT NULL,
    operation text NOT NULL,
    idempotency_key text NOT NULL,
    payload_hash text NOT NULL,
    response jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (principal_id, project_id, operation, idempotency_key)
);
CREATE TABLE messages (
    message_id uuid PRIMARY KEY,
    project_id text NOT NULL,
    sender text NOT NULL REFERENCES principals(principal_id),
    recipient text NOT NULL REFERENCES principals(principal_id),
    subject text NOT NULL,
    body text NOT NULL,
    category text NOT NULL,
    urgency text NOT NULL,
    task_id text,
    reply_to uuid,
    accepted_at timestamptz NOT NULL DEFAULT now(),
    delivered_at timestamptz,
    handled_at timestamptz,
    replied_at timestamptz,
    expires_at timestamptz,
    UNIQUE (project_id, message_id),
    FOREIGN KEY (project_id, task_id) REFERENCES tasks(project_id, task_id),
    FOREIGN KEY (project_id, reply_to) REFERENCES messages(project_id, message_id)
);
CREATE INDEX tasks_order ON tasks (project_id, priority, task_id);
CREATE INDEX messages_pending ON messages (project_id, recipient, accepted_at, message_id) WHERE handled_at IS NULL;
