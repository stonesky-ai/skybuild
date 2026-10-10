"""Explicit qualification of a dedicated database role; never called at API startup.

This module changes no passwords, ownership, memberships, or role attributes. An
administrator must create a clean LOGIN role and migrate the database first.
"""

import re
from pathlib import Path

from psycopg import sql


# No automatic grants on future objects. A schema change must update this policy.
READ_ONLY = {"schema_migrations", "principals", "principal_grants", "ledger_imports"}
APPEND_ONLY = {"task_journal", "cord_journal", "effect_journal", "claim_journal",
               "cpu_journal", "observation_events", "task_lineage", "idempotency",
               "task_usage_events", "cpu_worker_observations"}
MUTABLE = {"tasks", "messages", "task_readiness", "task_effects", "task_claims",
           "cpu_pools", "cpu_reservations", "observation_projections",
           "cpu_fake_dispatches", "cpu_fake_receipts", "cpu_worker_dispatches"}
TABLES = READ_ONLY | APPEND_ONLY | MUTABLE | {"task_dependencies"}
PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
# Only these reviewed helpers may lock read-only security/authority rows.
LOCK_ROUTINES = {"lock_principal": "TABLE(principal_id text, is_admin boolean)", "lock_ledger_import": "boolean"}


def _lock_body(name, through_version):
    migrations = Path(__file__).with_name("migrations")
    matches = []
    for migration in sorted(migrations.glob("*.sql")):
        if int(migration.name.split("_", 1)[0]) > through_version:
            continue
        source = migration.read_text()
        matches.extend(re.findall(
            r"CREATE(?: OR REPLACE)? FUNCTION " + name + r"\(.*?AS \$\$(.*?)\$\$;",
            source, re.DOTALL,
        ))
    if not matches:
        raise ValueError("Missing canonical lock helper")
    return " ".join(matches[-1].split())


def _identity(connection, expected_database, role):
    if not re.fullmatch(r"skybuild(?:_[a-zA-Z0-9_]+)?", expected_database):
        raise ValueError("Expected a dedicated skybuild database name")
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]{0,62}", role):
        raise ValueError("Invalid runtime role name")
    actual = connection.execute("SELECT current_database(), current_user").fetchone()
    if actual[0] != expected_database:
        raise ValueError("Database identity does not match configuration")
    if actual[1] == role:
        raise ValueError("Use a separate administrator connection for qualification")
    row = connection.execute("SELECT oid, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, "
                             "rolreplication, rolbypassrls FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
    if not row or not row[1] or any(row[2:]):
        raise ValueError("Runtime role must be an existing restricted LOGIN role")
    if connection.execute("SELECT 1 FROM pg_auth_members WHERE member = %s", (row[0],)).fetchone():
        raise ValueError("Runtime role must have no role memberships")
    if connection.execute("SELECT 1 FROM pg_shdepend WHERE refclassid = 'pg_authid'::regclass "
                          "AND refobjid = %s AND deptype = 'o'", (row[0],)).fetchone():
        raise ValueError("Runtime role must not own databases or schema objects")
    return row[0]


def _relations(connection):
    return connection.execute("SELECT c.oid, c.relname, c.relkind, n.nspname FROM pg_class c "
                              "JOIN pg_namespace n ON n.oid = c.relnamespace "
                              "WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema' "
                              "AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f') ORDER BY n.nspname, c.relname").fetchall()


def _expected(table):
    result = {"SELECT"}
    if table not in READ_ONLY:
        result.add("INSERT")
    if table in MUTABLE:
        result.add("UPDATE")
    if table == "task_dependencies":
        result.add("DELETE")
    return result


def audit_runtime_role(connection, expected_database, role):
    """Return effective privilege findings without changing any database state.

    Catalog queries run as the administrator, but all has_*_privilege calls
    explicitly inspect the runtime role, including privileges from PUBLIC.
    """
    runtime_oid = _identity(connection, expected_database, role)
    findings = []
    for name, privilege in connection.execute(
        "SELECT p.parname, a.privilege_type FROM pg_parameter_acl p, LATERAL aclexplode(p.paracl) a "
        "WHERE a.grantee IN (0, %s)", (runtime_oid,),
    ):
        findings.append(f"parameter privilege: {name} {privilege}")
    if not connection.execute("SELECT has_database_privilege(%s, current_database(), 'CONNECT')", (role,)).fetchone()[0]:
        findings.append("missing database CONNECT")
    if connection.execute("SELECT has_database_privilege(%s, current_database(), 'CONNECT WITH GRANT OPTION')", (role,)).fetchone()[0]:
        findings.append("database CONNECT grant option")
    for privilege in ("CREATE", "TEMP"):
        if connection.execute("SELECT has_database_privilege(%s, current_database(), %s)", (role, privilege)).fetchone()[0]:
            findings.append("excess database " + privilege)
    for oid, name in connection.execute("SELECT oid, nspname FROM pg_namespace"):
        if connection.execute("SELECT has_schema_privilege(%s, %s, 'CREATE')", (role, oid)).fetchone()[0]:
            findings.append("schema CREATE: " + name)
        if name == "skybuild" and not connection.execute("SELECT has_schema_privilege(%s, %s, 'USAGE')", (role, oid)).fetchone()[0]:
            findings.append("missing schema USAGE")
        if connection.execute("SELECT has_schema_privilege(%s, %s, 'USAGE WITH GRANT OPTION')", (role, oid)).fetchone()[0]:
            findings.append("schema USAGE grant option: " + name)
    seen = set()
    for oid, table, kind, schema in _relations(connection):
        expected = _expected(table) if schema == "skybuild" and table in TABLES and kind in ("r", "p") else set()
        if expected:
            seen.add(table)
        privileges = ("USAGE", "SELECT", "UPDATE") if kind == "S" else PRIVILEGES
        for privilege in privileges:
            function = "has_sequence_privilege" if kind == "S" else "has_table_privilege"
            granted = connection.execute(sql.SQL("SELECT {}(%s, %s, %s)").format(sql.SQL(function)), (role, oid, privilege)).fetchone()[0]
            if granted != (privilege in expected):
                findings.append(f"{'excess' if granted else 'missing'} {schema}.{table} {privilege}")
            if connection.execute(sql.SQL("SELECT {}(%s, %s, %s)").format(sql.SQL(function)),
                                  (role, oid, privilege + ' WITH GRANT OPTION')).fetchone()[0]:
                findings.append(f"table grant option: {schema}.{table} {privilege}")
        if kind == "S":
            continue
        # Table-level checks alone miss column grants, including grant options.
        for column, in connection.execute("SELECT attname FROM pg_attribute WHERE attrelid = %s AND attnum > 0 AND NOT attisdropped", (oid,)):
            for privilege in ("SELECT", "INSERT", "UPDATE", "REFERENCES"):
                allowed = privilege in expected
                granted = connection.execute("SELECT has_column_privilege(%s, %s, %s, %s)", (role, oid, column, privilege)).fetchone()[0]
                if granted != allowed:
                    findings.append(f"{'excess' if granted else 'missing'} {schema}.{table}.{column} {privilege}")
                if connection.execute("SELECT has_column_privilege(%s, %s, %s, %s)", (role, oid, column, privilege + ' WITH GRANT OPTION')).fetchone()[0]:
                    findings.append(f"grant option: {schema}.{table}.{column} {privilege}")
    findings.extend("missing table: " + table for table in sorted(TABLES - seen))
    # Default catalog reads and pg_settings updates are ordinary session access;
    # pg_settings uses PostgreSQL's own setting permission checks. Other catalog
    # writes could bypass the application DDL checks.
    for table, privilege in connection.execute(
        "SELECT DISTINCT c.relname, p.privilege FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "CROSS JOIN unnest(ARRAY['INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER']) p(privilege) "
        "WHERE n.nspname IN ('pg_catalog', 'information_schema') AND c.relkind IN ('r','p','v') "
        "AND (((c.oid <> 'pg_catalog.pg_settings'::regclass OR p.privilege <> 'UPDATE') AND "
        "(has_table_privilege(%s, c.oid, p.privilege) OR CASE WHEN p.privilege IN "
        "('INSERT','UPDATE','REFERENCES') THEN has_any_column_privilege(%s, c.oid, p.privilege) ELSE false END)) "
        "OR EXISTS (SELECT 1 FROM aclexplode(c.relacl) a WHERE a.grantee IN (0, %s) "
        "AND a.privilege_type = p.privilege AND NOT (c.oid = 'pg_catalog.pg_settings'::regclass "
        "AND p.privilege = 'UPDATE' AND a.grantee = 0 AND NOT a.is_grantable)) "
        "OR EXISTS (SELECT 1 FROM pg_attribute att, "
        "LATERAL aclexplode(att.attacl) a WHERE att.attrelid = c.oid AND a.grantee IN (0, %s) "
        "AND a.privilege_type = p.privilege AND NOT (c.oid = 'pg_catalog.pg_settings'::regclass "
        "AND p.privilege = 'UPDATE' AND a.grantee = 0 AND NOT a.is_grantable)))",
        (role, role, runtime_oid, runtime_oid),
    ):
        findings.append(f"system catalog write: {table} {privilege}")
    seen_routines = set()
    version = connection.execute("SELECT COALESCE(max(version), 0) FROM skybuild.schema_migrations").fetchone()[0]
    for oid, schema, name, body, definer, config, language, result, volatility in connection.execute(
        "SELECT p.oid, n.nspname, p.proname, p.prosrc, p.prosecdef, p.proconfig, l.lanname, "
        "pg_get_function_result(p.oid), p.provolatile FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_language l ON l.oid = p.prolang "
        "WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'"):
        # Resolve overload identity by argument types, not by function name alone.
        allowed = schema == "skybuild" and name in LOCK_ROUTINES and connection.execute(
            "SELECT %s::oid = to_regprocedure(%s)::oid", (oid, f"skybuild.{name}(text)")).fetchone()[0]
        granted = connection.execute("SELECT has_function_privilege(%s, %s, 'EXECUTE')", (role, oid)).fetchone()[0]
        if allowed:
            seen_routines.add(name)
            if (not definer or config != ["search_path=pg_catalog"] or language != "sql" or
                    result != LOCK_ROUTINES[name] or volatility != "v" or
                    " ".join(body.split()) != _lock_body(name, version)):
                findings.append("unsafe lock routine definition: " + name)
        if granted != bool(allowed):
            findings.append(f"{'excess' if granted else 'missing'} routine EXECUTE: {schema}.{name}")
        if connection.execute("SELECT has_function_privilege(%s, %s, 'EXECUTE WITH GRANT OPTION')", (role, oid)).fetchone()[0]:
            findings.append(f"routine grant option: {schema}.{name}")
    findings.extend("missing lock routine: " + name for name in sorted(LOCK_ROUTINES.keys() - seen_routines))
    # Preserve initdb's ordinary PUBLIC function access, not later grants to
    # restricted server utilities. pg_init_privs records nondefault initdb ACLs.
    for name, granted, grantable, baseline in connection.execute(
        "SELECT n.nspname || '.' || p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')', "
        "has_function_privilege(%s, p.oid, 'EXECUTE'), "
        "has_function_privilege(%s, p.oid, 'EXECUTE WITH GRANT OPTION'), "
        "EXISTS (SELECT 1 FROM aclexplode(COALESCE(i.initprivs, acldefault('f', p.proowner))) a "
        "WHERE a.grantee = 0 AND a.privilege_type = 'EXECUTE') "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "LEFT JOIN pg_init_privs i ON i.objoid = p.oid AND i.classoid = 'pg_proc'::regclass AND i.objsubid = 0 "
        "WHERE n.nspname IN ('pg_catalog', 'information_schema')", (role, role),
    ):
        if granted and not baseline:
            findings.append("system routine EXECUTE: " + name)
        if grantable:
            findings.append("system routine grant option: " + name)
    return {"ok": not findings, "database": expected_database, "role": role, "findings": findings}


def provision_runtime_role(connection, expected_database, role):
    """Apply the explicit policy in a caller-owned transaction, then audit it.

    PUBLIC privileges are revoked only in the dedicated target database. Do not
    run this against a shared application database. Existing unrelated direct
    grants cause audit failure and transaction rollback rather than silent repair.
    """
    _identity(connection, expected_database, role)
    relations = _relations(connection)
    actual = {name for _, name, kind, schema in relations if schema == "skybuild" and kind in ("r", "p")}
    if actual != TABLES:
        raise ValueError("Migrate the expected SkyBuild schema before provisioning")
    target = sql.Identifier(role)
    database = sql.Identifier(expected_database)
    connection.execute(sql.SQL("REVOKE CREATE, TEMPORARY ON DATABASE {} FROM PUBLIC").format(database))
    connection.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM {}").format(database, target))
    connection.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(database, target))
    for schema in ("public", "skybuild"):
        connection.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC, {}").format(sql.Identifier(schema), target))
    connection.execute(sql.SQL("GRANT USAGE ON SCHEMA skybuild TO {}").format(target))
    connection.execute(sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA skybuild FROM PUBLIC, {}").format(target))
    connection.execute(sql.SQL("REVOKE ALL ON ALL SEQUENCES IN SCHEMA skybuild FROM PUBLIC, {}").format(target))
    connection.execute(sql.SQL("REVOKE ALL ON ALL FUNCTIONS IN SCHEMA skybuild FROM PUBLIC, {}").format(target))
    for table in sorted(TABLES):
        privileges = sql.SQL(', ').join(sql.SQL(item) for item in sorted(_expected(table)))
        connection.execute(sql.SQL("GRANT {} ON TABLE skybuild.{} TO {}").format(privileges, sql.Identifier(table), target))
    for name in LOCK_ROUTINES:
        connection.execute(sql.SQL("GRANT EXECUTE ON FUNCTION skybuild.{}(text) TO {}").format(sql.Identifier(name), target))
    result = audit_runtime_role(connection, expected_database, role)
    if not result["ok"]:
        raise ValueError("Runtime role has unexpected privileges: " + "; ".join(result["findings"]))
    return result
