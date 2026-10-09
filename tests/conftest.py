"""Disposable role fixture; never reads the live SKYBUILD_DSN."""

import os
import secrets
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import pytest

from skybuild.runtime_role import provision_runtime_role
from skybuild.store import Store


@pytest.fixture
def restricted_database():
    dsn = os.environ.get("SKYBUILD_HTTP_TEST_DSN")
    if not dsn:
        pytest.skip("Set SKYBUILD_HTTP_TEST_DSN to a task-owned disposable database")
    database = conninfo_to_dict(dsn).get("dbname", "")
    if not database.startswith("skybuild_") or not database.endswith("_test"):
        pytest.fail("Runtime role tests require a skybuild_*_test disposable database")
    Store(dsn, database).migrate()
    role, password = "runtime_" + uuid4().hex, secrets.token_urlsafe(32)
    with psycopg.connect(dsn) as connection:
        connection.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(role), sql.Literal(password)))
    try:
        with psycopg.connect(dsn) as connection:
            provision_runtime_role(connection, database, role)
        yield dsn, make_conninfo(dsn, user=role, password=password), database, role
    finally:
        with psycopg.connect(dsn) as connection:
            connection.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            connection.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
