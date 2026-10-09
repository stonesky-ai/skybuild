-- Launch-free project CPU pool. Central and local restrictions compose.
CREATE TABLE cpu_pools (
    project_id text PRIMARY KEY,
    capacity integer NOT NULL CHECK (capacity >= 0),
    enabled boolean NOT NULL,
    generation bigint NOT NULL CHECK (generation > 0),
    local_enabled boolean NOT NULL DEFAULT false,
    local_generation bigint NOT NULL DEFAULT 1 CHECK (local_generation > 0)
);
CREATE TABLE cpu_reservations (
    action_id text PRIMARY KEY,
    attempt_id text NOT NULL UNIQUE,
    project_id text NOT NULL REFERENCES cpu_pools(project_id),
    task_id text NOT NULL,
    actor text NOT NULL REFERENCES principals(principal_id),
    claim_fence bigint NOT NULL CHECK (claim_fence > 0),
    task_revision bigint NOT NULL CHECK (task_revision > 0),
    readiness_generation bigint NOT NULL CHECK (readiness_generation > 0),
    generation bigint NOT NULL CHECK (generation > 0),
    local_generation bigint NOT NULL CHECK (local_generation > 0),
    units integer NOT NULL CHECK (units > 0),
    intent_hash text NOT NULL,
    state text NOT NULL DEFAULT 'reserved' CHECK (state IN ('reserved', 'cancelled')),
    FOREIGN KEY (project_id, task_id) REFERENCES tasks(project_id, task_id)
);
CREATE TABLE cpu_journal (
    event_id uuid PRIMARY KEY,
    project_id text NOT NULL,
    actor text NOT NULL REFERENCES principals(principal_id),
    action text NOT NULL,
    reason text NOT NULL,
    before_state jsonb,
    after_state jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER cpu_journal_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON cpu_journal
    FOR EACH STATEMENT EXECUTE FUNCTION refuse_journal_mutation();
CREATE FUNCTION guard_cpu_reservation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP <> 'UPDATE' THEN
        RAISE EXCEPTION 'CPU action identities cannot be deleted or truncated';
    END IF;
    IF OLD.state <> 'reserved' OR NEW.state <> 'cancelled' OR
       (to_jsonb(OLD) - 'state') <> (to_jsonb(NEW) - 'state') THEN
        RAISE EXCEPTION 'CPU reservation identity is immutable; only cancellation is allowed';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER cpu_reservation_guard BEFORE UPDATE OR DELETE ON cpu_reservations
    FOR EACH ROW EXECUTE FUNCTION guard_cpu_reservation();
CREATE TRIGGER cpu_reservation_truncate BEFORE TRUNCATE ON cpu_reservations
    FOR EACH STATEMENT EXECUTE FUNCTION guard_cpu_reservation();
CREATE FUNCTION guard_cpu_pool() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP <> 'UPDATE' THEN
        RAISE EXCEPTION 'CPU control generations cannot be deleted or truncated';
    END IF;
    IF NEW.project_id <> OLD.project_id OR NOT (
       (NEW.generation = OLD.generation + 1 AND NEW.local_generation = OLD.local_generation
        AND NEW.local_enabled = OLD.local_enabled) OR
       (NEW.local_generation = OLD.local_generation + 1 AND NEW.generation = OLD.generation
        AND NEW.enabled = OLD.enabled AND NEW.capacity = OLD.capacity)) THEN
        RAISE EXCEPTION 'Exactly one CPU control generation must advance';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER cpu_pool_guard BEFORE UPDATE OR DELETE ON cpu_pools
    FOR EACH ROW EXECUTE FUNCTION guard_cpu_pool();
CREATE TRIGGER cpu_pool_truncate BEFORE TRUNCATE ON cpu_pools
    FOR EACH STATEMENT EXECUTE FUNCTION guard_cpu_pool();
