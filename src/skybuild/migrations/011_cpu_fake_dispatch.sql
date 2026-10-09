-- A database simulator only. These records authorize no operating-system I/O.
ALTER TABLE task_effects ADD COLUMN adapter_kind text NOT NULL DEFAULT 'unqualified'
    CHECK (adapter_kind IN ('unqualified', 'cpu-fake-v1'));
ALTER TABLE task_effects DROP CONSTRAINT task_effects_state_check;
ALTER TABLE task_effects ADD CHECK (state IN ('intent', 'unknown', 'cancelled', 'settled'));
ALTER TABLE task_effects DROP CONSTRAINT task_effects_check;
ALTER TABLE task_effects ADD CHECK (exposure_held = (state IN ('intent', 'unknown')));
ALTER TABLE effect_journal DROP CONSTRAINT effect_journal_action_check;
ALTER TABLE effect_journal ADD CHECK (action IN ('intent', 'unknown', 'cancelled', 'settled'));
ALTER TABLE cpu_reservations DROP CONSTRAINT cpu_reservations_state_check;
ALTER TABLE cpu_reservations ADD CHECK (state IN ('reserved', 'cancelled', 'released'));

CREATE TABLE cpu_fake_dispatches (
    operation_id text PRIMARY KEY REFERENCES task_effects(operation_id),
    action_id text NOT NULL UNIQUE REFERENCES cpu_reservations(action_id),
    reservation_hash text NOT NULL,
    process_identity uuid NOT NULL UNIQUE,
    state text NOT NULL DEFAULT 'unknown' CHECK (state IN ('unknown', 'stop-pending', 'settled')),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE cpu_fake_receipts (
    operation_id text PRIMARY KEY REFERENCES cpu_fake_dispatches(operation_id),
    process_identity uuid NOT NULL UNIQUE,
    reservation_hash text NOT NULL,
    state text NOT NULL CHECK (state IN ('running', 'terminal')),
    starts integer NOT NULL CHECK (starts IN (0, 1)),
    CHECK (state <> 'running' OR starts = 1)
);

CREATE FUNCTION guard_cpu_fake_dispatch() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'unknown' OR NOT EXISTS (
            SELECT 1 FROM cpu_reservations r JOIN task_effects e
                ON e.operation_id = NEW.operation_id
            WHERE r.action_id = NEW.action_id AND r.state = 'reserved'
                AND r.intent_hash = NEW.reservation_hash
                AND e.adapter_kind = 'cpu-fake-v1' AND e.state = 'unknown' AND e.exposure_held
                AND e.project_id = r.project_id AND e.task_id = r.task_id
                AND e.attempt_id = r.attempt_id AND e.task_revision = r.task_revision
                AND e.authority_epoch = 1 AND e.authority_generation = r.generation
                AND e.allocation_refs = ARRAY[r.action_id]
        ) THEN RAISE EXCEPTION 'Fake dispatch requires an exact fake effect and held reservation'; END IF;
        RETURN NEW;
    END IF;
    IF TG_OP <> 'UPDATE' THEN RAISE EXCEPTION 'Fake dispatch identities are permanent'; END IF;
    IF (to_jsonb(OLD) - 'state') <> (to_jsonb(NEW) - 'state') OR NOT (
        (OLD.state = 'unknown' AND NEW.state = 'stop-pending') OR
        (OLD.state IN ('unknown', 'stop-pending') AND NEW.state = 'settled' AND EXISTS (
            SELECT 1 FROM cpu_fake_receipts p WHERE p.operation_id = OLD.operation_id
                AND p.process_identity = OLD.process_identity
                AND p.reservation_hash = OLD.reservation_hash AND p.state = 'terminal'
        ))
    ) THEN RAISE EXCEPTION 'Fake dispatch identity or terminal proof is invalid'; END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER cpu_fake_dispatch_guard BEFORE INSERT OR UPDATE OR DELETE ON cpu_fake_dispatches
    FOR EACH ROW EXECUTE FUNCTION guard_cpu_fake_dispatch();
CREATE TRIGGER cpu_fake_dispatch_truncate BEFORE TRUNCATE ON cpu_fake_dispatches
    FOR EACH STATEMENT EXECUTE FUNCTION guard_cpu_fake_dispatch();

CREATE FUNCTION guard_cpu_fake_receipt() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NOT EXISTS (SELECT 1 FROM cpu_fake_dispatches d
            WHERE d.operation_id = NEW.operation_id AND d.process_identity = NEW.process_identity
                AND d.reservation_hash = NEW.reservation_hash AND
                ((d.state = 'unknown' AND NEW.state = 'running') OR
                 (d.state = 'stop-pending' AND NEW.state = 'terminal' AND NEW.starts = 0)))
        THEN RAISE EXCEPTION 'Fake receipt requires its durable bound dispatch intent'; END IF;
        RETURN NEW;
    END IF;
    IF TG_OP <> 'UPDATE' THEN RAISE EXCEPTION 'Fake receipt identities are permanent'; END IF;
    IF (to_jsonb(OLD) - 'state') <> (to_jsonb(NEW) - 'state') OR
       OLD.state <> 'running' OR NEW.state <> 'terminal' OR NOT EXISTS (
           SELECT 1 FROM cpu_fake_dispatches d WHERE d.operation_id = OLD.operation_id
               AND d.state = 'stop-pending')
    THEN RAISE EXCEPTION 'Only a requested fake stop may create terminal proof'; END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER cpu_fake_receipt_guard BEFORE INSERT OR UPDATE OR DELETE ON cpu_fake_receipts
    FOR EACH ROW EXECUTE FUNCTION guard_cpu_fake_receipt();
CREATE TRIGGER cpu_fake_receipt_truncate BEFORE TRUNCATE ON cpu_fake_receipts
    FOR EACH STATEMENT EXECUTE FUNCTION guard_cpu_fake_receipt();

CREATE OR REPLACE FUNCTION guard_effect_record() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP <> 'UPDATE' THEN RAISE EXCEPTION 'Effect records cannot be deleted or truncated'; END IF;
    IF (to_jsonb(OLD) - 'state' - 'exposure_held') IS DISTINCT FROM
       (to_jsonb(NEW) - 'state' - 'exposure_held') OR NOT (
        (OLD.state = 'intent' AND NEW.state IN ('unknown', 'cancelled')) OR
        (OLD.adapter_kind = 'cpu-fake-v1' AND OLD.state = 'unknown' AND NEW.state = 'settled'
         AND EXISTS (SELECT 1 FROM cpu_fake_dispatches d JOIN cpu_fake_receipts p USING (operation_id)
             WHERE d.operation_id = OLD.operation_id AND d.state = 'settled'
                 AND p.state = 'terminal' AND p.process_identity = d.process_identity
                 AND p.reservation_hash = d.reservation_hash))
    ) THEN RAISE EXCEPTION 'Effect identity and terminal observations are immutable'; END IF;
    RETURN NEW;
END;
$$;
CREATE OR REPLACE FUNCTION guard_cpu_reservation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP <> 'UPDATE' THEN RAISE EXCEPTION 'CPU action identities cannot be deleted or truncated'; END IF;
    IF OLD.state <> 'reserved' OR (to_jsonb(OLD) - 'state') <> (to_jsonb(NEW) - 'state') OR NOT (
        (NEW.state = 'cancelled' AND NOT EXISTS (
            SELECT 1 FROM cpu_fake_dispatches d WHERE d.action_id = OLD.action_id)) OR
        (NEW.state = 'released' AND EXISTS (
            SELECT 1 FROM cpu_fake_dispatches d JOIN task_effects e USING (operation_id)
                JOIN cpu_fake_receipts p USING (operation_id)
            WHERE d.action_id = OLD.action_id AND d.reservation_hash = OLD.intent_hash
                AND d.state = 'settled' AND e.adapter_kind = 'cpu-fake-v1'
                AND e.state = 'settled' AND NOT e.exposure_held AND p.state = 'terminal'
                AND p.process_identity = d.process_identity AND p.reservation_hash = d.reservation_hash))
    ) THEN RAISE EXCEPTION 'CPU reservation identity or release proof is invalid'; END IF;
    RETURN NEW;
END;
$$;
