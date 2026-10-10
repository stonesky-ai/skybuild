-- Dedicated signed integration producer. Migration 014 is the CPU dispatch
-- predecessor in the combined release; existing grants remain valid.
ALTER TABLE principal_grants DROP CONSTRAINT principal_grants_operation_check;
ALTER TABLE principal_grants ADD CONSTRAINT principal_grants_operation_check
    CHECK (operation IN ('tasks:read', 'tasks:write', 'tasks:claim',
                        'cord:send', 'cord:read', 'cord:handle', 'integration:attest'));
