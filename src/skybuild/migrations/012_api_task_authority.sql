ALTER TABLE ledger_imports DROP CONSTRAINT ledger_imports_authority_check;
ALTER TABLE ledger_imports ADD CONSTRAINT ledger_imports_authority_check
    CHECK (authority IN ('markdown', 'api'));

CREATE OR REPLACE FUNCTION lock_ledger_import(p_project_id text)
RETURNS boolean
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog
AS $$
    SELECT COALESCE(
        (SELECT authority = 'markdown' FROM skybuild.ledger_imports AS i
         WHERE i.project_id = p_project_id FOR SHARE),
        true
    );
$$;
REVOKE ALL ON FUNCTION lock_ledger_import(text) FROM PUBLIC;
