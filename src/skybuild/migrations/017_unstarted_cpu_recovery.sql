-- Owner-attested recovery of a launch intent that cannot start late.
-- Historical dispatch, effect and reservation identities remain permanent.
ALTER TABLE cpu_worker_dispatches DROP CONSTRAINT cpu_worker_dispatches_state_check;
ALTER TABLE cpu_worker_dispatches ADD CONSTRAINT cpu_worker_dispatches_state_check
    CHECK (state IN ('prepared', 'launch-intent', 'running', 'unknown', 'terminal', 'settled', 'cancelled'));
ALTER TABLE cpu_worker_dispatches ADD CONSTRAINT cpu_worker_cancelled_no_invocation
    CHECK (state <> 'cancelled' OR invocation_id IS NULL);

CREATE TABLE cpu_worker_recoveries (
    recovery_id uuid PRIMARY KEY,
    operation_id text NOT NULL UNIQUE REFERENCES cpu_worker_dispatches(operation_id),
    action_id text NOT NULL UNIQUE REFERENCES cpu_reservations(action_id),
    actor text NOT NULL REFERENCES principals(principal_id),
    request_digest text NOT NULL CHECK (request_digest ~ '^[0-9a-f]{64}$'),
    evidence jsonb NOT NULL CHECK (jsonb_typeof(evidence) = 'object'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE FUNCTION guard_cpu_worker_recovery() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'CPU worker recovery receipts are append-only'; END IF;
    IF NOT EXISTS (
        SELECT 1 FROM cpu_worker_dispatches d JOIN cpu_reservations r USING (action_id)
            JOIN task_effects e USING (operation_id)
            JOIN tasks t ON t.project_id = d.project_id AND t.task_id = d.task_id
            JOIN task_claims c ON c.project_id = d.project_id AND c.task_id = d.task_id
        WHERE d.operation_id = NEW.operation_id AND d.action_id = NEW.action_id
          AND d.state = 'launch-intent' AND d.invocation_id IS NULL
          AND NEW.evidence->>'reservation_hash' = d.reservation_hash
          AND NEW.evidence->>'attempt_id' = d.attempt_id
          AND NEW.evidence->>'claim_fence' = d.claim_fence::text
          AND NEW.evidence->>'expected_revision' = t.revision::text
          AND c.fence = d.claim_fence AND c.task_revision = d.claim_task_revision
          AND c.holder = r.actor
          AND t.metadata #>> '{_skybuild_workflow,petri,token,attempt_id}' = d.attempt_id
          AND t.metadata #>> '{_skybuild_workflow,petri,token,claim_fence}' = d.claim_fence::text
          AND t.metadata #>> '{_skybuild_workflow,petri,token,place}' = 'working'
          AND NEW.evidence #>> '{proof,host_id}' = d.host_id
          AND NEW.evidence #>> '{proof,unit_name}' = d.unit_name
          AND NEW.evidence #>> '{proof,launch_nonce}' = d.launch_nonce
          AND NEW.evidence->'proof' @> '{"launcher_stopped":true,"launcher_cgroup_empty":true,
              "launcher_fenced":true,"unit_absent":true,"container_absent":true,"manifest_absent":true}'::jsonb
          AND NEW.evidence #>> '{proof,fence_sha256}' ~ '^[0-9a-f]{64}$'
          AND NEW.evidence #>> '{proof,evidence_sha256}' ~ '^[0-9a-f]{64}$'
          AND NEW.evidence #>> '{proof,controller_invocation_id}' ~ '^[0-9a-f]{32}$'
          AND NEW.evidence #>> '{proof,controller_invocation_id}' <> repeat('0', 32)
          AND length(NEW.evidence #>> '{proof,controller_unit}') BETWEEN 1 AND 255
          AND r.state = 'reserved' AND r.intent_hash = d.reservation_hash
          AND r.attempt_id = d.attempt_id AND r.claim_fence = d.claim_fence
          AND e.adapter_kind = 'cpu-worker-v1' AND e.state = 'unknown' AND e.exposure_held
          AND NOT EXISTS (SELECT 1 FROM cpu_worker_observations o WHERE o.operation_id = d.operation_id)
    ) OR NOT EXISTS (SELECT 1 FROM principals WHERE principal_id = NEW.actor AND is_admin)
    THEN RAISE EXCEPTION 'CPU worker recovery requires an owner and an unstarted held intent'; END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER cpu_worker_recovery_guard BEFORE INSERT OR UPDATE OR DELETE ON cpu_worker_recoveries
    FOR EACH ROW EXECUTE FUNCTION guard_cpu_worker_recovery();
CREATE TRIGGER cpu_worker_recovery_truncate BEFORE TRUNCATE ON cpu_worker_recoveries
    FOR EACH STATEMENT EXECUTE FUNCTION guard_cpu_worker_recovery();

CREATE OR REPLACE FUNCTION guard_cpu_worker_dispatch() RETURNS trigger LANGUAGE plpgsql AS $$
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
        (OLD.state = 'launch-intent' AND NEW.state = 'cancelled'
         AND OLD.invocation_id IS NULL AND NEW.invocation_id IS NULL
         AND EXISTS (SELECT 1 FROM cpu_worker_recoveries p
             WHERE p.operation_id = OLD.operation_id AND p.action_id = OLD.action_id)
         AND NOT EXISTS (SELECT 1 FROM cpu_worker_observations o
             WHERE o.operation_id = OLD.operation_id)) OR
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

CREATE OR REPLACE FUNCTION guard_effect_record() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP <> 'UPDATE' THEN RAISE EXCEPTION 'Effect records cannot be deleted or truncated'; END IF;
    IF (to_jsonb(OLD) - 'state' - 'exposure_held') IS DISTINCT FROM
       (to_jsonb(NEW) - 'state' - 'exposure_held') OR NOT (
        (OLD.adapter_kind = 'cpu-worker-v1' AND OLD.state = 'unknown'
         AND OLD.exposure_held AND NEW.state = 'cancelled' AND NOT NEW.exposure_held
         AND EXISTS (SELECT 1 FROM cpu_worker_dispatches d JOIN cpu_worker_recoveries p USING (operation_id)
             WHERE d.operation_id = OLD.operation_id AND d.action_id = p.action_id
               AND d.state = 'cancelled' AND d.invocation_id IS NULL)) OR
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
        (NEW.state = 'cancelled' AND EXISTS (
            SELECT 1 FROM cpu_worker_dispatches d JOIN cpu_worker_recoveries p USING (operation_id)
                JOIN task_effects e USING (operation_id)
            WHERE d.action_id = OLD.action_id AND p.action_id = OLD.action_id
              AND d.reservation_hash = OLD.intent_hash AND d.state = 'cancelled'
              AND d.invocation_id IS NULL AND e.adapter_kind = 'cpu-worker-v1'
              AND e.state = 'cancelled' AND NOT e.exposure_held)) OR
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
