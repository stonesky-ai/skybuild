"""Exact 012-to-013 rehearsal; never connects to an accepted runtime."""

import hashlib
import os
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.types.json import Jsonb
import pytest

from skybuild.api import create_app
from skybuild.runtime_role import audit_runtime_role, provision_runtime_role
from skybuild.store import Store


@pytest.fixture
def schema_012_database():
    base = os.environ.get("SKYBUILD_HTTP_TEST_DSN")
    if not base:
        pytest.skip("Requires the guarded disposable PostgreSQL gate")
    name = conninfo_to_dict(base).get("dbname", "")
    if not name.startswith("skybuild_") or not name.endswith("_test"):
        pytest.fail("Promotion rehearsal requires a disposable skybuild_*_test database")
    database, role = "skybuild_petri_promotion_" + uuid4().hex + "_test", "runtime_" + uuid4().hex
    password = uuid4().hex + uuid4().hex
    admin = make_conninfo(base, dbname=database)
    runtime = make_conninfo(admin, user=role, password=password)
    paths = sorted((Path(__file__).parents[1] / "src/skybuild/migrations").glob("*.sql"))
    prefix = [path for path in paths if int(path.name.split("_", 1)[0]) <= 12]
    expected = [(int(path.name.split("_", 1)[0]), hashlib.sha256(path.read_bytes()).hexdigest())
                for path in prefix]
    assert [version for version, _ in expected] == list(range(1, 13))
    with psycopg.connect(base, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    try:
        with psycopg.connect(base) as connection:
            connection.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}")
                               .format(sql.Identifier(role), sql.Literal(password)))
        with psycopg.connect(admin) as connection:
            connection.execute("CREATE SCHEMA skybuild")
            connection.execute("SET LOCAL search_path TO skybuild, pg_catalog")
            connection.execute("CREATE TABLE schema_migrations (version integer PRIMARY KEY, digest text NOT NULL)")
            for path, (version, digest) in zip(prefix, expected, strict=True):
                connection.execute(path.read_text())
                connection.execute("INSERT INTO schema_migrations VALUES (%s, %s)", (version, digest))
            assert provision_runtime_role(connection, database, role)["ok"]
        yield admin, runtime, database, role, expected
    finally:
        with psycopg.connect(base, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))
            connection.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


class Accepted012Store(Store):
    """Model deployed e0cc07c2a6fa72e1aff1bcc7b3bf93bcd2a443e2 in tests only.

    This fixture models its exact schema/readiness and journal write columns;
    it does not execute or qualify the accepted container binary.
    """

    @staticmethod
    def _new_task_metadata(task):
        return task["metadata"]

    @staticmethod
    def _journal(connection, principal, after, before=None, *, operation=None, reason=None, event_facts=None):
        assert event_facts is None, "Accepted schema 012 cannot write Petri event facts"
        operation = operation or ("updated" if before else "created")
        if operation == "created":
            connection.execute("INSERT INTO task_readiness (project_id, task_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                               (after["project_id"], after["task_id"]))
        connection.execute(
            "INSERT INTO task_journal (event_id, project_id, task_id, actor, operation, revision, reason, before_state, after_state) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (uuid4(), after["project_id"], after["task_id"], principal.principal_id, operation,
             after["revision"], reason or "Task " + operation, Jsonb(before) if before else None, Jsonb(after)))

    def readiness(self):
        with self._connection() as connection:
            actual = connection.execute("SELECT version, digest FROM schema_migrations ORDER BY version").fetchall()
        if [(row["version"], row["digest"]) for row in actual] != self.expected_prefix:
            raise RuntimeError("Accepted schema-012 controller refuses incompatible schema")
        return {"ready": True, "schema_version": 12}


def test_schema_012_atomic_upgrade_preserves_history_and_recovers(schema_012_database):
    admin, runtime, database, role, prefix = schema_012_database
    registry = Store(admin, database)
    owner, worker, project = "owner-" + uuid4().hex, "worker-" + uuid4().hex, "project-" + uuid4().hex
    owner_token, worker_token = uuid4().hex + uuid4().hex, uuid4().hex + uuid4().hex
    registry.provision_principal(owner, owner_token, is_admin=True)
    registry.provision_principal(worker, worker_token, grants={project: [
        "tasks:read", "tasks:write", "cord:read", "cord:send", "cord:handle"]})
    with registry._connection() as connection:
        connection.execute(
            "INSERT INTO ledger_imports (project_id, commit_id, content_sha256, import_sha256, "
            "task_count, status_counts, authority) VALUES (%s, 'test-api', %s, %s, 0, '{}'::jsonb, 'api')",
            (project, "0" * 64, "1" * 64))
    accepted = Accepted012Store(runtime, database)
    accepted.expected_prefix = prefix
    api = f"/api/v1/projects/{project}"

    def headers(token=owner_token, **extra):
        return {"Authorization": "Bearer " + token, "Idempotency-Key": uuid4().hex, **extra}

    def snapshot():
        with psycopg.connect(admin) as connection:
            return tuple(connection.execute(query).fetchall() for query in (
                "SELECT to_jsonb(t) FROM skybuild.tasks t ORDER BY task_id",
                "SELECT to_jsonb(j) - 'event_facts' FROM skybuild.task_journal j ORDER BY event_id",
                "SELECT to_jsonb(m) FROM skybuild.messages m ORDER BY message_id"))

    migration = Path(__file__).parents[1] / "src/skybuild/migrations/013_petri_workflow.sql"
    digest = hashlib.sha256(migration.read_bytes()).hexdigest()

    def upgrade(*, bad_privilege=False):
        with psycopg.connect(admin) as connection:
            connection.execute("SET LOCAL search_path TO skybuild, pg_catalog")
            connection.execute("SET LOCAL statement_timeout = '30s'")
            connection.execute("SET LOCAL lock_timeout = '5s'")
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended('skybuild:migrate', 0))")
            assert connection.execute("SELECT current_database()").fetchone()[0] == database
            assert connection.execute("SELECT version, digest FROM schema_migrations ORDER BY version").fetchall() == prefix
            assert connection.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = %s AND usename = %s",
                                      (database, role)).fetchone()[0] == 0
            connection.execute(migration.read_text())
            connection.execute("INSERT INTO schema_migrations VALUES (13, %s)", (digest,))
            assert provision_runtime_role(connection, database, role)["ok"]
            if bad_privilege:
                connection.execute(sql.SQL("GRANT UPDATE ON task_journal TO {}").format(sql.Identifier(role)))
            audit = audit_runtime_role(connection, database, role)
            if not audit["ok"]:
                assert "excess skybuild.task_journal UPDATE" in audit["findings"]
                raise RuntimeError("Candidate role qualification failed")

    with TestClient(create_app(accepted)) as client:
        assert client.get("/health/ready").json() == {"status": "ready"}
        for task_id in ("ambiguous", "legacy-done"):
            response = client.post(api + "/tasks", headers=headers(), json={
                "task_id": task_id, "title": "Retained legacy task", "description": "Legacy scope"})
            assert response.status_code == 201, response.text
        # A representative historical completion snapshot, not a new completion
        # attestation. The existing journal rows stay immutable.
        principal = accepted.authenticate(owner_token)
        before = accepted.get_task(principal, project, "legacy-done")
        with accepted._connection() as connection:
            connection.execute("UPDATE tasks SET status = 'done', phase = 'done', revision = revision + 1, "
                               "metadata = %s WHERE project_id = %s AND task_id = 'legacy-done'",
                               (Jsonb({"historical_completion_receipt": "legacy-accepted"}), project))
            after = accepted._task(connection, project, "legacy-done")
            accepted._journal(connection, principal, after, before, operation="completed",
                              reason="Representative pre-Petri completion snapshot")
        message = client.post(api + "/cord/messages", headers=headers(), json={
            "recipient": worker, "subject": "Pinned assignment", "body": "Retain this message"})
        assert message.status_code == 201, message.text
        retained = snapshot()
        inbox = client.get(api + "/cord/inbox", headers=headers(worker_token)).json()
        assert len(inbox) == 1
        with psycopg.connect(admin) as connection:
            assert audit_runtime_role(connection, database, role)["ok"]
        assert accepted.readiness() == {"ready": True, "schema_version": 12}
        assert len(digest) == 64
        assert client.get("/health/ready").json() == {"status": "ready"}
        assert snapshot() == retained
        with pytest.raises(RuntimeError, match="Candidate role qualification failed"):
            upgrade(bad_privilege=True)
        with psycopg.connect(admin) as connection:
            assert connection.execute("SELECT version, digest FROM skybuild.schema_migrations ORDER BY version").fetchall() == prefix
            assert connection.execute("SELECT count(*) FROM pg_constraint WHERE conrelid = 'skybuild.tasks'::regclass "
                                      "AND conname = 'tasks_petri_workflow_shape'").fetchone()[0] == 0
            assert connection.execute("SELECT count(*) FROM information_schema.columns WHERE table_schema = 'skybuild' "
                                      "AND ((table_name = 'task_journal' AND column_name = 'event_facts') OR "
                                      "(table_name = 'cpu_reservations' AND column_name = 'claim_task_revision'))").fetchone()[0] == 0
            assert audit_runtime_role(connection, database, role)["ok"]
        assert client.get("/health/ready").json() == {"status": "ready"}
        assert client.get(api + "/cord/inbox", headers=headers(worker_token)).json() == inbox
        assert snapshot() == retained

    # Stop using the accepted writer before committing the incompatible schema.
    upgrade()
    with pytest.raises(RuntimeError, match="incompatible schema"):
        accepted.readiness()
    assert registry.readiness() == {"ready": True, "schema_version": 16}
    assert snapshot() == retained
    with psycopg.connect(admin) as connection:
        assert audit_runtime_role(connection, database, role)["ok"]
        assert connection.execute("SELECT count(*) FROM skybuild.task_journal WHERE event_facts IS NOT NULL").fetchone()[0] == 0

    class FailedCandidateStore(Store):
        def readiness(self):
            raise RuntimeError("Injected candidate readiness failure")

    with TestClient(create_app(FailedCandidateStore(runtime, database))) as client:
        assert client.get("/health/ready").status_code == 503
    assert snapshot() == retained
    with TestClient(create_app(Store(runtime, database))) as client:
        assert client.get("/health/ready").json() == {"status": "ready"}
        assert client.get(api + "/cord/inbox", headers=headers(worker_token)).json() == inbox
        for task_id in ("ambiguous", "legacy-done"):
            path = api + "/tasks/" + task_id
            old = client.get(path, headers=headers()).json()
            response = client.post(path + "/workflow", headers=headers(**{"If-Match": str(old["revision"])}),
                                   json={"event": "initialize"})
            assert response.status_code == 200, response.text
            assert response.json()["token"]["place"] == "hold"
            assert response.json()["token"]["hold_reason"]
            assert response.json()["task"]["status"] == old["status"]
        history = client.get(api + "/tasks/legacy-done/history", headers=headers()).json()
        assert history[-2]["operation"] == "completed"
        assert history[-1]["operation"] == "workflow_initialized"
        with psycopg.connect(admin) as connection:
            rows = connection.execute("SELECT to_jsonb(j) - 'event_facts' FROM skybuild.task_journal j "
                                      "WHERE operation <> 'workflow_initialized' ORDER BY event_id").fetchall()
            assert rows == retained[1]
        created = client.post(api + "/tasks", headers=headers(), json={
            "task_id": "new-petri", "title": "New task", "description": "Current scope",
            "acceptance_criteria": ["Verify candidate behavior"]})
        assert created.status_code == 201, created.text
        workflow = client.get(api + "/tasks/new-petri/workflow", headers=headers(worker_token))
        assert workflow.status_code == 200, workflow.text
        assert workflow.json()["token"]["place"] == "ready"
        assert workflow.json()["token"]["policy_version"] == "petri-checks-v1"
