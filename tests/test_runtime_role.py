"""Effective database authority tests using only disposable PostgreSQL."""

import psycopg
from psycopg import sql
import pytest

from skybuild.contracts import DomainError
from skybuild.runtime_role import audit_runtime_role, provision_runtime_role
from skybuild.store import Store


@pytest.mark.parametrize("statement", [
    "CREATE SCHEMA runtime_escape",
    "CREATE TABLE skybuild.runtime_escape (id integer)",
    "CREATE TABLE public.runtime_escape (id integer)",
    "CREATE TEMP TABLE runtime_escape (id integer)",
    "ALTER TABLE skybuild.tasks ADD COLUMN runtime_escape integer",
    "ALTER TABLE skybuild.task_journal DISABLE TRIGGER ALL",
    "DROP TABLE skybuild.task_journal",
    "TRUNCATE skybuild.task_journal",
    "UPDATE skybuild.task_journal SET reason = 'rewrite'",
    "DELETE FROM skybuild.cord_journal",
    "UPDATE skybuild.principals SET is_admin = true",
    "UPDATE skybuild.principals SET principal_id = 'replacement'",
    "DELETE FROM skybuild.principal_grants",
    "INSERT INTO skybuild.ledger_imports VALUES ('escape', '', '', '', 0, '{}', 'markdown')",
    "UPDATE skybuild.ledger_imports SET authority = 'markdown'",
    "UPDATE skybuild.schema_migrations SET digest = 'rewrite'",
    "CREATE ROLE runtime_escape",
    "CREATE DATABASE runtime_escape",
    "SET session_replication_role = replica",
    "SET ROLE postgres",
    "CREATE OR REPLACE FUNCTION skybuild.lock_principal(text) RETURNS TABLE(principal_id text, is_admin boolean) LANGUAGE sql AS 'SELECT NULL::text, true'",
])
def test_runtime_cannot_escape_policy(restricted_database, statement):
    _, dsn, _, _ = restricted_database
    with psycopg.connect(dsn, autocommit=True) as connection:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(statement)


def test_runtime_login_readiness_and_migration_denial(restricted_database):
    admin_dsn, runtime_dsn, database, role = restricted_database
    store = Store(runtime_dsn, database)
    assert store.readiness()["ready"]
    with psycopg.connect(runtime_dsn) as connection:
        assert connection.execute("SELECT current_user").fetchone()[0] == role
    with pytest.raises(DomainError) as failure:
        store.migrate()
    assert isinstance(failure.value.__cause__, psycopg.errors.InsufficientPrivilege)
    with pytest.raises(DomainError):
        store.provision_principal("escape", "x" * 64, is_admin=True)
    with psycopg.connect(admin_dsn) as connection:
        assert audit_runtime_role(connection, database, role)["ok"]
        assert provision_runtime_role(connection, database, role)["ok"]


def test_runtime_cannot_delegate_grants(restricted_database):
    admin_dsn, dsn, _, _ = restricted_database
    # PostgreSQL emits a warning and grants nothing rather than failing GRANT.
    with psycopg.connect(dsn) as connection:
        connection.execute("GRANT SELECT ON skybuild.tasks TO PUBLIC")
    with psycopg.connect(admin_dsn) as connection:
        assert not connection.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_class c, LATERAL aclexplode(c.relacl) a "
            "WHERE c.oid = 'skybuild.tasks'::regclass AND a.grantee = 0)"
        ).fetchone()[0]


@pytest.mark.parametrize("operation", ["claims", "admission", "observations", "effects"])
def test_runtime_supports_launch_free_store_operations(restricted_database, operation):
    from test_store import actors
    from test_claims import test_concurrent_claims_one_winner_and_stable_replay
    from test_admission import test_atomic_capacity_and_explicit_cancel_replay
    from test_observations import test_replay_reordering_restart_and_identity_pinning
    from test_effects import test_stable_operation_duplicate_and_conflicting_identity

    admin_dsn, runtime_dsn, database, _ = restricted_database
    # Only identity provisioning uses the administrator. Every operation below
    # opens real connections as the restricted LOGIN role, including races.
    identities = actors.__wrapped__(Store(admin_dsn, database))
    operations = {
        "claims": test_concurrent_claims_one_winner_and_stable_replay,
        "admission": test_atomic_capacity_and_explicit_cancel_replay,
        "observations": test_replay_reordering_restart_and_identity_pinning,
        "effects": test_stable_operation_duplicate_and_conflicting_identity,
    }
    operations[operation](Store(runtime_dsn, database), identities)


@pytest.mark.parametrize("grant,expected", [
    ("GRANT UPDATE ON skybuild.task_journal TO {role}", "task_journal UPDATE"),
    ("GRANT UPDATE (is_admin) ON skybuild.principals TO {role}", "principals.is_admin UPDATE"),
    ("GRANT SELECT ON skybuild.tasks TO {role} WITH GRANT OPTION", "grant option"),
    ("GRANT CREATE ON SCHEMA public TO PUBLIC", "schema CREATE"),
    ("GRANT TEMP ON DATABASE {database} TO PUBLIC", "database TEMP"),
    ("GRANT CONNECT ON DATABASE {database} TO {role} WITH GRANT OPTION", "CONNECT grant option"),
    ("GRANT USAGE ON SCHEMA skybuild TO {role} WITH GRANT OPTION", "USAGE grant option"),
    ("GRANT CREATE ON SCHEMA information_schema TO {role}", "schema CREATE"),
    ("GRANT UPDATE ON pg_catalog.pg_class TO {role}", "system catalog write"),
    ("GRANT EXECUTE ON FUNCTION skybuild.refuse_journal_mutation() TO PUBLIC", "routine EXECUTE"),
])
def test_audit_detects_effective_privilege_drift(restricted_database, grant, expected):
    admin_dsn, _, database, role = restricted_database
    with psycopg.connect(admin_dsn) as connection:
        try:
            connection.execute(sql.SQL(grant).format(role=sql.Identifier(role), database=sql.Identifier(database)))
            result = audit_runtime_role(connection, database, role)
            assert not result["ok"]
            assert any(expected in finding for finding in result["findings"])
        finally:
            connection.rollback()


@pytest.mark.parametrize("attribute", ["SUPERUSER", "CREATEDB", "CREATEROLE", "BYPASSRLS", "REPLICATION"])
def test_audit_rejects_unsafe_role_attributes(restricted_database, attribute):
    admin_dsn, _, database, role = restricted_database
    with psycopg.connect(admin_dsn) as connection:
        try:
            connection.execute(sql.SQL("ALTER ROLE {} " + attribute).format(sql.Identifier(role)))
            with pytest.raises(ValueError, match="restricted LOGIN"):
                audit_runtime_role(connection, database, role)
        finally:
            connection.rollback()


def test_audit_rejects_membership_and_ownership(restricted_database):
    admin_dsn, _, database, role = restricted_database
    with psycopg.connect(admin_dsn) as connection:
        try:
            connection.execute(sql.SQL("GRANT pg_read_all_data TO {}").format(sql.Identifier(role)))
            with pytest.raises(ValueError, match="memberships"):
                audit_runtime_role(connection, database, role)
        finally:
            connection.rollback()
        try:
            connection.execute(sql.SQL("CREATE SCHEMA owned_escape AUTHORIZATION {}").format(sql.Identifier(role)))
            with pytest.raises(ValueError, match="must not own"):
                audit_runtime_role(connection, database, role)
        finally:
            connection.rollback()


def test_lock_helpers_preserve_row_locks_and_ignore_search_path(restricted_database):
    admin_dsn, runtime_dsn, database, _ = restricted_database
    admin = Store(admin_dsn, database)
    admin.provision_principal("lock-test", "z" * 64)
    with psycopg.connect(admin_dsn) as connection:
        connection.execute("INSERT INTO skybuild.ledger_imports VALUES ('lock-test', '', '', '', 0, '{}', 'markdown')")
    with psycopg.connect(runtime_dsn) as runtime, psycopg.connect(admin_dsn, autocommit=True) as owner:
        runtime.execute("SET LOCAL search_path = public")
        assert runtime.execute("SELECT * FROM skybuild.lock_principal('lock-test')").fetchone() == ("lock-test", False)
        assert runtime.execute("SELECT skybuild.lock_ledger_import('lock-test')").fetchone() == (True,)
        for table, column in (("principals", "principal_id"), ("ledger_imports", "project_id")):
            with pytest.raises(psycopg.errors.LockNotAvailable):
                owner.execute(sql.SQL("SELECT 1 FROM skybuild.{} WHERE {} = 'lock-test' FOR UPDATE NOWAIT").format(sql.Identifier(table), sql.Identifier(column)))
        runtime.rollback()
        owner.execute("DELETE FROM skybuild.ledger_imports WHERE project_id = 'lock-test'")
        owner.execute("DELETE FROM skybuild.principals WHERE principal_id = 'lock-test'")


def test_wrong_database_identity_rejected_before_mutation(restricted_database):
    admin_dsn, _, _, role = restricted_database
    with psycopg.connect(admin_dsn) as connection:
        with pytest.raises(ValueError, match="identity"):
            provision_runtime_role(connection, "skybuild_other_test", role)


def test_audit_rejects_modified_lock_helper(restricted_database):
    admin_dsn, _, database, role = restricted_database
    with psycopg.connect(admin_dsn) as connection:
        try:
            connection.execute("ALTER FUNCTION skybuild.lock_principal(text) SET search_path = public")
            result = audit_runtime_role(connection, database, role)
            assert "unsafe lock routine definition: lock_principal" in result["findings"]
        finally:
            connection.rollback()


def test_unknown_table_has_no_automatic_runtime_grant(restricted_database):
    admin_dsn, _, database, role = restricted_database
    with psycopg.connect(admin_dsn) as connection:
        try:
            connection.execute("CREATE TABLE skybuild.new_policy_required (id integer)")
            assert not connection.execute(
                "SELECT has_table_privilege(%s, 'skybuild.new_policy_required', 'INSERT')", (role,)
            ).fetchone()[0]
            with pytest.raises(ValueError, match="expected SkyBuild schema"):
                provision_runtime_role(connection, database, role)
        finally:
            connection.rollback()
