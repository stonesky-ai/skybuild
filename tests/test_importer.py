"""Frozen ledger import checks against task-owned disposable PostgreSQL."""

import json
import os
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import pytest
from fastapi.testclient import TestClient

from skybuild.api import create_app
from skybuild.contracts import DomainError
from skybuild.importer import import_frozen, prepare_import
from skybuild.store import Store

ROOT = Path(__file__).parents[1]
LEDGERS = ROOT / "docs/design"
CONTRACT = LEDGERS / "implementation/frozen_ledger_import.json"


def apply(store, plan, contract=CONTRACT, expected=None):
    return import_frozen(store, "skybuild", LEDGERS, contract, expected or plan["import_sha256"])


@pytest.fixture
def plan():
    return prepare_import(LEDGERS, CONTRACT)


@pytest.fixture
def fresh_store():
    base = os.environ.get("SKYBUILD_IMPORT_TEST_DSN")
    if not base:
        pytest.skip("Set SKYBUILD_IMPORT_TEST_DSN to a task-owned disposable PostgreSQL database")
    name = conninfo_to_dict(base).get("dbname", "")
    if name != "skybuild_import_test":
        pytest.fail("Importer tests require skybuild_import_test as the base database")
    database = "skybuild_import_test_" + uuid4().hex
    with psycopg.connect(base, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    try:
        store = Store(make_conninfo(base, dbname=database), database)
        store.migrate()
        yield store
    finally:
        with psycopg.connect(base, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))


def test_frozen_plan_preserves_all_sources_and_rejects_changed_bytes(plan, tmp_path):
    assert plan["task_count"] == 28
    assert plan["counts"] == {"in-progress": 5, "proposed": 9, "deferred": 12, "done": 2}
    assert all(record["description"].startswith("## " + record["task_id"]) for record in plan["records"])
    assert any(record["dependencies"] for record in plan["records"])
    assert next(record for record in plan["records"] if record["task_id"] == "SKYBUILD-REPOSITORY")["status"] == "done"
    for name in ("mastertodo.md", "deferred.md", "alreadydone.md"):
        (tmp_path / name).write_bytes((LEDGERS / name).read_bytes())
    target = tmp_path / "mastertodo.md"
    target.write_bytes(target.read_bytes() + b"\n")
    with pytest.raises(DomainError, match="Ledger bytes differ"):
        prepare_import(tmp_path, CONTRACT)


def test_atomic_import_replay_api_and_restart(plan, fresh_store):
    store = fresh_store
    result = apply(store, plan)
    assert result == {"result": "imported", "project_id": "skybuild", "task_count": 28, "authority": "markdown"}
    assert apply(store, plan)["result"] == "unchanged"
    token = uuid4().hex + uuid4().hex
    store.provision_principal("test-owner", token, is_admin=True)
    with TestClient(create_app(store)) as client:
        response = client.get("/api/v1/projects/skybuild/tasks/SKYBUILD-REPOSITORY", headers={"Authorization": "Bearer " + token})
        assert response.status_code == 200
        assert "Evidence: commit 2da489" in response.json()["description"]
        history = client.get("/api/v1/projects/skybuild/tasks/SKYBUILD-REPOSITORY/history", headers={"Authorization": "Bearer " + token})
        assert len(history.json()) == 1
        assert history.json()[0]["operation"] == "imported"
        denied = client.post("/api/v1/projects/skybuild/tasks", headers={"Authorization": "Bearer " + token, "Idempotency-Key": "no-write"},
                             json={"task_id": "new", "title": "new", "description": "new"})
        assert denied.status_code == 409
        assert denied.json()["error"]["code"] == "authority"
    restarted = Store(store.dsn, store.expected_database)
    assert restarted.readiness()["schema_version"] == 3
    assert apply(restarted, plan)["result"] == "unchanged"
    with store._connection() as connection:
        assert connection.execute("SELECT authority FROM ledger_imports").fetchone()["authority"] == "markdown"
        assert connection.execute("SELECT count(*) AS count FROM task_dependencies").fetchone()["count"] == sum(len(r["dependencies"]) for r in plan["records"])


def test_conflicting_source_and_changed_destination_are_refused(plan, fresh_store):
    store = fresh_store
    apply(store, plan)
    with pytest.raises(DomainError, match="reviewed hash"):
        apply(store, plan, expected="0" * 64)
    with store._connection() as connection:
        connection.execute("UPDATE tasks SET description = 'changed' WHERE task_id = 'SKYBUILD-REPOSITORY'")
    with pytest.raises(DomainError, match="destination changed"):
        apply(store, plan)
    with store._connection() as connection:
        original = next(record["description"] for record in plan["records"] if record["task_id"] == "SKYBUILD-REPOSITORY")
        connection.execute("UPDATE tasks SET description = %s WHERE task_id = 'SKYBUILD-REPOSITORY'", (original,))
        connection.execute("INSERT INTO messages (message_id, project_id, sender, recipient, subject, body, category, urgency) VALUES (%s, 'skybuild', 'skybuild-ledger-import', 'skybuild-ledger-import', 'extra', 'extra', 'misc', 'normal')", (uuid4(),))
    with pytest.raises(DomainError, match="destination changed"):
        apply(store, plan)


def test_nonempty_destination_and_mid_import_failure_roll_back(plan, fresh_store):
    store = fresh_store
    with store._connection() as connection:
        connection.execute("INSERT INTO principals VALUES ('preexisting', 'verifier', true)")
        connection.execute("INSERT INTO tasks (project_id, task_id, title, description, status, phase, responsible, next_action) VALUES ('other', 'one', 'one', 'one', 'proposed', 'triage', 'owner', 'review')")
    with pytest.raises(DomainError, match="unrelated data"):
        apply(store, plan)
    with store._connection() as connection:
        connection.execute("DELETE FROM tasks WHERE project_id = 'other'")
        connection.execute("CREATE FUNCTION fail_import() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'forced failure'; END; $$")
        connection.execute("CREATE TRIGGER fail_import BEFORE INSERT ON task_journal FOR EACH ROW EXECUTE FUNCTION fail_import()")
    with pytest.raises(DomainError) as error:
        apply(store, plan)
    assert error.value.code == "unavailable"
    with store._connection() as connection:
        for table in ("tasks", "task_journal", "task_dependencies", "ledger_imports"):
            assert connection.execute(sql.SQL("SELECT count(*) AS count FROM {}").format(sql.Identifier(table))).fetchone()["count"] == 0
        assert not connection.execute("SELECT 1 FROM principals WHERE principal_id = 'skybuild-ledger-import'").fetchone()
        connection.execute("DROP TRIGGER fail_import ON task_journal")
        connection.execute("DROP FUNCTION fail_import()")
    assert apply(store, plan)["result"] == "imported"


def test_mapping_change_requires_a_new_reviewed_hash(plan, fresh_store, tmp_path):
    contract = json.loads(CONTRACT.read_text())
    contract["dependencies"]["SKYBUILD-TASK-CUTOVER"] = []
    changed_path = tmp_path / "mapping.json"
    changed_path.write_text(json.dumps(contract))
    with pytest.raises(DomainError, match="reviewed hash"):
        apply(fresh_store, plan, changed_path)
    with fresh_store._connection() as connection:
        assert connection.execute("SELECT count(*) AS count FROM tasks").fetchone()["count"] == 0
