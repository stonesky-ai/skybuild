-- Owner-only natural-completion dispatch. This binds existing reservations;
-- it adds no capacity ledger and authorizes no physical stop.
ALTER TABLE task_effects ADD COLUMN IF NOT EXISTS adapter_kind text NOT NULL DEFAULT 'unqualified';
ALTER TABLE task_effects DROP CONSTRAINT IF EXISTS task_effects_adapter_kind_check;
ALTER TABLE task_effects ADD CONSTRAINT task_effects_adapter_kind_check
    CHECK (adapter_kind IN ('unqualified', 'cpu-fake-v1', 'cpu-worker-v1'));

CREATE TABLE cpu_worker_dispatches (
    operation_id text PRIMARY KEY REFERENCES task_effects(operation_id),
    action_id text NOT NULL UNIQUE REFERENCES cpu_reservations(action_id),
    reservation_hash text NOT NULL CHECK (reservation_hash ~ '^[0-9a-f]{64}$'),
    profile_id text NOT NULL CHECK (profile_id = 'bounded-trusted-cpu-patch-v1'),
    project_id text NOT NULL,
    task_id text NOT NULL,
    attempt_id text NOT NULL,
    task_revision bigint NOT NULL CHECK (task_revision > 0),
    claim_fence bigint NOT NULL CHECK (claim_fence > 0),
    claim_task_revision bigint NOT NULL CHECK (claim_task_revision > 0),
    readiness_generation bigint NOT NULL CHECK (readiness_generation > 0),
    pool_generation bigint NOT NULL CHECK (pool_generation > 0),
    local_generation bigint NOT NULL CHECK (local_generation > 0),
    host_id text NOT NULL CHECK (length(host_id) BETWEEN 1 AND 255),
    worker_id text NOT NULL,
    unit_name text NOT NULL UNIQUE CHECK (unit_name ~ '^skybuild-job-[0-9a-f]{24}\.service$'),
    launch_nonce text NOT NULL UNIQUE CHECK (launch_nonce ~ '^[0-9a-f]{32}$'),
    source_digest text NOT NULL CHECK (source_digest ~ '^[0-9a-f]{64}$'),
    controller_head text NOT NULL CHECK (controller_head ~ '^[0-9a-f]{40}$'),
    controller_source_digest text NOT NULL CHECK (controller_source_digest ~ '^[0-9a-f]{64}$'),
    controller_profile_digest text NOT NULL CHECK (controller_profile_digest ~ '^[0-9a-f]{64}$'),
    interpreter_digest text NOT NULL CHECK (interpreter_digest ~ '^[0-9a-f]{64}$'),
    permit_digest text NOT NULL CHECK (permit_digest ~ '^[0-9a-f]{64}$'),
    assignment_digest text NOT NULL CHECK (assignment_digest ~ '^[0-9a-f]{64}$'),
    patch_digest text NOT NULL CHECK (patch_digest ~ '^[0-9a-f]{64}$'),
    argv_digest text NOT NULL CHECK (argv_digest ~ '^[0-9a-f]{64}$'),
    approved_until timestamptz NOT NULL,
    state text NOT NULL DEFAULT 'prepared'
        CHECK (state IN ('prepared', 'launch-intent', 'running', 'unknown', 'terminal', 'settled')),
    invocation_id text CHECK (invocation_id ~ '^[0-9a-f]{32}$' AND invocation_id <> repeat('0', 32)),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (project_id, task_id) REFERENCES tasks(project_id, task_id),
    CHECK (state NOT IN ('running', 'terminal', 'settled') OR invocation_id IS NOT NULL)
);

CREATE TABLE cpu_worker_observations (
    observation_id uuid PRIMARY KEY,
    operation_id text NOT NULL REFERENCES cpu_worker_dispatches(operation_id),
    action_id text NOT NULL,
    host_id text NOT NULL,
    unit_name text NOT NULL,
    launch_nonce text NOT NULL,
    invocation_id text NOT NULL CHECK (invocation_id ~ '^[0-9a-f]{32}$' AND invocation_id <> repeat('0', 32)),
    sequence bigint NOT NULL CHECK (sequence > 0),
    phase text NOT NULL CHECK (phase IN ('running', 'unknown', 'failed', 'completed')),
    result text,
    exit_status integer,
    worker_result_digest text CHECK (worker_result_digest IS NULL OR worker_result_digest ~ '^[0-9a-f]{64}$'),
    observed_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (operation_id, sequence),
    CHECK ((phase = 'completed' AND result IS NOT NULL AND exit_status IS NOT NULL)
        OR (phase = 'failed' AND result IS NOT NULL)
        OR (phase IN ('running', 'unknown') AND result IS NULL AND exit_status IS NULL)),
    CHECK (phase <> 'completed' OR (result = 'success' AND exit_status = 0 AND worker_result_digest IS NOT NULL)),
    CHECK (phase <> 'failed' OR worker_result_digest IS NULL)
);

CREATE FUNCTION guard_cpu_worker_dispatch() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'prepared' OR NEW.invocation_id IS NOT NULL OR NOT EXISTS (
            SELECT 1 FROM cpu_reservations r JOIN task_effects e USING (project_id, task_id)
            WHERE r.action_id = NEW.action_id AND r.project_id = NEW.project_id
              AND r.task_id = NEW.task_id AND r.attempt_id = NEW.attempt_id
              AND r.state = 'reserved' AND r.intent_hash = NEW.reservation_hash
              AND r.task_revision = NEW.task_revision AND r.claim_fence = NEW.claim_fence
              AND r.claim_task_revision = NEW.claim_task_revision
              AND r.readiness_generation = NEW.readiness_generation
              AND r.generation = NEW.pool_generation AND r.local_generation = NEW.local_generation
              AND e.operation_id = NEW.operation_id AND e.adapter_kind = 'cpu-worker-v1'
              AND e.state = 'unknown' AND e.exposure_held
              AND e.attempt_id = NEW.attempt_id AND e.task_revision = NEW.task_revision
              AND e.authority_generation = NEW.pool_generation
              AND e.allocation_refs = ARRAY[NEW.action_id]
        ) THEN RAISE EXCEPTION 'CPU worker dispatch requires its exact held effect and reservation'; END IF;
        RETURN NEW;
    END IF;
    IF TG_OP <> 'UPDATE' THEN RAISE EXCEPTION 'CPU worker dispatch identities are permanent'; END IF;
    IF (to_jsonb(OLD) - 'state' - 'invocation_id') IS DISTINCT FROM
       (to_jsonb(NEW) - 'state' - 'invocation_id') OR NOT (
        (OLD.state = 'prepared' AND NEW.state = 'launch-intent' AND NEW.invocation_id IS NULL) OR
        (OLD.state = 'launch-intent' AND NEW.state IN ('running', 'unknown') AND
             (NEW.state <> 'running' OR NEW.invocation_id IS NOT NULL)) OR
        (OLD.state = 'running' AND NEW.state IN ('running', 'unknown') AND
             NEW.invocation_id = OLD.invocation_id) OR
        (OLD.state IN ('running', 'unknown') AND NEW.state = 'terminal' AND
             NEW.invocation_id = OLD.invocation_id AND EXISTS (
                SELECT 1 FROM cpu_worker_observations o WHERE o.operation_id = OLD.operation_id
                  AND o.phase = 'completed' AND o.result = 'success' AND o.exit_status = 0
                  AND o.action_id = OLD.action_id AND o.host_id = OLD.host_id
                  AND o.unit_name = OLD.unit_name AND o.launch_nonce = OLD.launch_nonce
                  AND o.invocation_id = OLD.invocation_id)) OR
        (OLD.state = 'unknown' AND NEW.state IN ('unknown', 'running') AND
             (OLD.invocation_id IS NULL OR NEW.invocation_id = OLD.invocation_id)) OR
        (OLD.state = 'terminal' AND NEW.state = 'settled' AND NEW.invocation_id = OLD.invocation_id
             AND EXISTS (SELECT 1 FROM cpu_worker_observations o WHERE o.operation_id = OLD.operation_id
                 AND o.phase = 'completed' AND o.result = 'success' AND o.exit_status = 0
                 AND o.action_id = OLD.action_id AND o.host_id = OLD.host_id
                 AND o.unit_name = OLD.unit_name AND o.launch_nonce = OLD.launch_nonce
                 AND o.invocation_id = OLD.invocation_id))
       ) THEN RAISE EXCEPTION 'CPU worker dispatch identity or state transition is invalid'; END IF;
    IF OLD.invocation_id IS NOT NULL AND NEW.invocation_id IS DISTINCT FROM OLD.invocation_id THEN
        RAISE EXCEPTION 'CPU worker invocation identity cannot change';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER cpu_worker_dispatch_guard BEFORE INSERT OR UPDATE OR DELETE ON cpu_worker_dispatches
    FOR EACH ROW EXECUTE FUNCTION guard_cpu_worker_dispatch();
CREATE TRIGGER cpu_worker_dispatch_truncate BEFORE TRUNCATE ON cpu_worker_dispatches
    FOR EACH STATEMENT EXECUTE FUNCTION guard_cpu_worker_dispatch();

CREATE FUNCTION guard_cpu_worker_observation() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE prior cpu_worker_observations%ROWTYPE;
BEGIN
    IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'CPU worker observations are append-only'; END IF;
    SELECT * INTO prior FROM cpu_worker_observations
      WHERE operation_id = NEW.operation_id ORDER BY sequence DESC LIMIT 1;
    IF (prior.operation_id IS NOT NULL AND NEW.sequence <> prior.sequence + 1)
       OR (prior.operation_id IS NULL AND NEW.sequence <> 1)
       OR NOT EXISTS (
        SELECT 1 FROM cpu_worker_dispatches d WHERE d.operation_id = NEW.operation_id
          AND d.action_id = NEW.action_id AND d.host_id = NEW.host_id
          AND d.unit_name = NEW.unit_name AND d.launch_nonce = NEW.launch_nonce
          AND d.invocation_id = NEW.invocation_id
          AND d.state IN ('running', 'unknown', 'terminal')
       ) THEN RAISE EXCEPTION 'CPU worker observation is out of sequence or identity'; END IF;
    IF prior.operation_id IS NOT NULL AND
       (prior.action_id, prior.host_id, prior.unit_name, prior.launch_nonce, prior.invocation_id)
       IS DISTINCT FROM
       (NEW.action_id, NEW.host_id, NEW.unit_name, NEW.launch_nonce, NEW.invocation_id) THEN
        RAISE EXCEPTION 'CPU worker observation identity changed';
    END IF;
    IF prior.phase IN ('completed', 'failed') THEN
        RAISE EXCEPTION 'Terminal CPU worker observation is immutable';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER cpu_worker_observation_guard BEFORE INSERT OR UPDATE OR DELETE ON cpu_worker_observations
    FOR EACH ROW EXECUTE FUNCTION guard_cpu_worker_observation();
CREATE TRIGGER cpu_worker_observation_truncate BEFORE TRUNCATE ON cpu_worker_observations
    FOR EACH STATEMENT EXECUTE FUNCTION guard_cpu_worker_observation();

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
                 AND p.reservation_hash = d.reservation_hash)) OR
        (OLD.adapter_kind = 'cpu-worker-v1' AND OLD.state = 'unknown' AND NEW.state = 'settled'
         AND EXISTS (SELECT 1 FROM cpu_worker_dispatches d JOIN cpu_worker_observations o USING (operation_id)
             WHERE d.operation_id = OLD.operation_id AND d.state = 'settled'
               AND o.phase = 'completed' AND o.result = 'success' AND o.exit_status = 0
               AND o.action_id = d.action_id AND o.host_id = d.host_id
               AND o.unit_name = d.unit_name AND o.launch_nonce = d.launch_nonce
               AND o.invocation_id = d.invocation_id))
    ) THEN RAISE EXCEPTION 'Effect identity and terminal observations are immutable'; END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION guard_cpu_reservation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP <> 'UPDATE' THEN RAISE EXCEPTION 'CPU action identities cannot be deleted or truncated'; END IF;
    IF OLD.state <> 'reserved' OR (to_jsonb(OLD) - 'state') <> (to_jsonb(NEW) - 'state') OR NOT (
        (NEW.state = 'cancelled' AND NOT EXISTS (
            SELECT 1 FROM cpu_fake_dispatches d WHERE d.action_id = OLD.action_id)
            AND NOT EXISTS (SELECT 1 FROM cpu_worker_dispatches d WHERE d.action_id = OLD.action_id)) OR
        (NEW.state = 'released' AND (
            EXISTS (SELECT 1 FROM cpu_fake_dispatches d JOIN task_effects e USING (operation_id)
                JOIN cpu_fake_receipts p USING (operation_id)
                WHERE d.action_id = OLD.action_id AND d.reservation_hash = OLD.intent_hash
                  AND d.state = 'settled' AND e.adapter_kind = 'cpu-fake-v1'
                  AND e.state = 'settled' AND NOT e.exposure_held AND p.state = 'terminal'
                  AND p.process_identity = d.process_identity AND p.reservation_hash = d.reservation_hash)
            OR EXISTS (SELECT 1 FROM cpu_worker_dispatches d JOIN task_effects e USING (operation_id)
                JOIN cpu_worker_observations o USING (operation_id)
                WHERE d.action_id = OLD.action_id AND d.reservation_hash = OLD.intent_hash
                  AND d.state = 'settled' AND e.adapter_kind = 'cpu-worker-v1'
                  AND e.state = 'settled' AND NOT e.exposure_held
                  AND o.phase = 'completed' AND o.result = 'success' AND o.exit_status = 0
                  AND o.action_id = d.action_id AND o.host_id = d.host_id
                  AND o.unit_name = d.unit_name AND o.launch_nonce = d.launch_nonce
                  AND o.invocation_id = d.invocation_id)))
    ) THEN RAISE EXCEPTION 'CPU reservation identity or release proof is invalid'; END IF;
    RETURN NEW;
END;
$$;
