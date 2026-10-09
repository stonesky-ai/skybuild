-- Runtime needs row locks without permission to rewrite credentials or import
-- authority. These narrowly scoped helpers preserve the existing locking rules.
CREATE FUNCTION lock_principal(p_principal_id text)
RETURNS TABLE (principal_id text, is_admin boolean)
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog
AS $$
    SELECT p.principal_id, p.is_admin FROM skybuild.principals AS p
    WHERE p.principal_id = p_principal_id FOR SHARE;
$$;
REVOKE ALL ON FUNCTION lock_principal(text) FROM PUBLIC;

CREATE FUNCTION lock_ledger_import(p_project_id text)
RETURNS boolean
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog
AS $$
    SELECT true FROM skybuild.ledger_imports AS i
    WHERE i.project_id = p_project_id AND i.authority = 'markdown' FOR SHARE;
$$;
REVOKE ALL ON FUNCTION lock_ledger_import(text) FROM PUBLIC;
