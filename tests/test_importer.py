"""Frozen ledger import checks against task-owned disposable PostgreSQL."""

import json
import os
import subprocess
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import time
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import pytest
from fastapi.testclient import TestClient

from skybuild.api import create_app
from skybuild.contracts import DomainError
from skybuild.importer import _workflow_fields, cutover_live, import_frozen, prepare_import
from skybuild.ledger import build_manifest
from skybuild.store import Store

ROOT = Path(__file__).parents[1]
LEDGERS = ROOT / "docs/design"
CONTRACT = LEDGERS / "implementation/frozen_ledger_import.json"
CURRENT_CONTRACT = LEDGERS / "implementation/current_task_import.json"
CURRENT_LEDGER_COMMIT = "d79d2e1947d2c8e9edb577ab5f5093edfa3c94e3"


def apply(store, plan, contract=CONTRACT, expected=None):
    return import_frozen(store, "skybuild", LEDGERS, contract, expected or plan["import_sha256"])


@pytest.fixture
def plan(tmp_path, monkeypatch):
    # Rehearse the identified freeze, not mutable live Markdown task authority.
    root = tmp_path / "frozen"
    ledger_dir = root / "docs/design"
    ledger_dir.mkdir(parents=True)
    commit = json.loads(CONTRACT.read_text())["commit"]
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    subprocess.run(["git", "fetch", "--quiet", str(ROOT), commit], cwd=root, check=True)
    for name in ("mastertodo.md", "deferred.md", "alreadydone.md"):
        blob = subprocess.run(["git", "show", f"{commit}:docs/design/{name}"], cwd=root,
                              capture_output=True, check=True).stdout
        (ledger_dir / name).write_bytes(blob)
    monkeypatch.setattr(sys.modules[__name__], "LEDGERS", ledger_dir)
    return prepare_import(ledger_dir, CONTRACT)


@pytest.fixture
def current_source(tmp_path):
    source = tmp_path / "frozen-ledgers"
    source.mkdir()
    for name in ("mastertodo.md", "deferred.md", "alreadydone.md"):
        blob = subprocess.run(["git", "show", f"{CURRENT_LEDGER_COMMIT}:docs/design/{name}"],
                              cwd=ROOT, capture_output=True, check=True).stdout
        (source / name).write_bytes(blob)
    return source


@pytest.fixture
def current_plan(current_source):
    return prepare_import(current_source, CURRENT_CONTRACT, repository=ROOT)


def postgres_system_identifier(dsn):
    with psycopg.connect(dsn) as connection:
        return str(connection.execute("SELECT system_identifier::text FROM pg_control_system()").fetchone()[0])


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


def test_workflow_projection_uses_explicit_labels_and_keeps_next_action_text():
    raw = ("## SKYBUILD-EXAMPLE — Example\n\n"
           "- Status: in-progress. Phase: review and publish. Responsible: lead dispatcher. "
           "Next action: rerun the gate; retain the exact result. Area: service.\n")
    assert _workflow_fields(raw, "in-progress") == {
        "phase": "review and publish", "responsible": "lead dispatcher",
        "next_action": "rerun the gate; retain the exact result", "assignee": None, "blocker": None,
    }


def test_contract_v2_requires_reviewed_workflow_for_every_frozen_task(plan, tmp_path):
    contract = json.loads(CONTRACT.read_text())
    manifest = build_manifest([LEDGERS / name for name in ("mastertodo.md", "deferred.md", "alreadydone.md")])
    contract["schema_version"] = 2
    contract["workflow"] = {task["task_id"]: _workflow_fields(task["raw"], task["status"])
                            for task in manifest["tasks"]}
    contract_path = tmp_path / "current-contract.json"
    contract_path.write_text(json.dumps(contract))
    result = prepare_import(LEDGERS, contract_path)
    assert result["task_count"] == 28
    assert result["records"][0]["phase"] == contract["workflow"][result["records"][0]["task_id"]]["phase"]
    del contract["workflow"][result["records"][0]["task_id"]]
    contract_path.write_text(json.dumps(contract))
    with pytest.raises(DomainError, match="Workflow mapping"):
        prepare_import(LEDGERS, contract_path)


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
    assert restarted.readiness()["schema_version"] == 12
    assert apply(restarted, plan)["result"] == "unchanged"
    with store._connection() as connection:
        assert connection.execute("SELECT authority FROM ledger_imports").fetchone()["authority"] == "markdown"
        assert connection.execute("SELECT count(*) AS count FROM task_dependencies").fetchone()["count"] == sum(len(r["dependencies"]) for r in plan["records"])


@pytest.mark.parametrize("project_id", ["skybuild", "missing-receipt-project"])
def test_missing_authority_receipt_fails_closed_for_every_project(fresh_store, project_id):
    token = uuid4().hex + uuid4().hex
    fresh_store.provision_principal("test-owner", token, is_admin=True)
    with TestClient(create_app(fresh_store)) as client:
        response = client.post(f"/api/v1/projects/{project_id}/tasks",
                               headers={"Authorization": "Bearer " + token, "Idempotency-Key": "no-receipt"},
                               json={"task_id": "new", "title": "new", "description": "new"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "authority"
    with fresh_store._connection() as connection:
        assert connection.execute("SELECT count(*) AS count FROM tasks").fetchone()["count"] == 0


def test_explicit_api_authority_receipt_allows_task_writes(fresh_store):
    with fresh_store._connection() as connection:
        connection.execute(
            "INSERT INTO ledger_imports (project_id, commit_id, content_sha256, import_sha256, "
            "task_count, status_counts, authority) VALUES ('skybuild', 'api', %s, %s, 0, '{}'::jsonb, 'api')",
            ("0" * 64, "1" * 64),
        )
    token = uuid4().hex + uuid4().hex
    fresh_store.provision_principal("test-owner", token, is_admin=True)
    with TestClient(create_app(fresh_store)) as client:
        response = client.post("/api/v1/projects/skybuild/tasks",
                               headers={"Authorization": "Bearer " + token, "Idempotency-Key": "api-owned"},
                               json={"task_id": "api-task", "title": "API task", "description": "A test task"})
    assert response.status_code == 201
    assert response.json()["task_id"] == "api-task"


def test_live_cutover_rehearsal_switches_authority_with_import(plan, fresh_store):
    database = "skybuild_pilot"
    with psycopg.connect(fresh_store.dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    try:
        live_dsn = make_conninfo(fresh_store.dsn, dbname=database)
        store = Store(live_dsn, database, postgres_system_identifier(live_dsn))
        store.migrate()
        token = uuid4().hex + uuid4().hex
        store.provision_principal("cutover-owner", token, is_admin=True)
        result = cutover_live(store, LEDGERS, CONTRACT, plan["import_sha256"])
        assert result == {"result": "imported", "project_id": "skybuild",
                          "task_count": 28, "authority": "api"}
        assert cutover_live(store, LEDGERS, CONTRACT, plan["import_sha256"])["result"] == "unchanged"
        with TestClient(create_app(store)) as client:
            headers = {"Authorization": "Bearer " + token}
            tasks = client.get("/api/v1/projects/skybuild/tasks", headers=headers)
            assert tasks.status_code == 200 and len(tasks.json()) == 28
            write = client.post("/api/v1/projects/skybuild/tasks", headers={**headers, "Idempotency-Key": "after-cutover"},
                                json={"task_id": "api-after-cutover", "title": "New API task", "description": "Writable only after the authority receipt commits"})
            assert write.status_code == 201
        with store._connection() as connection:
            assert connection.execute("SELECT authority FROM ledger_imports WHERE project_id = 'skybuild'").fetchone()["authority"] == "api"
    finally:
        with psycopg.connect(fresh_store.dsn, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))


def test_current_38_task_cutover_rehearsal_serves_frozen_tasks(current_plan, current_source, fresh_store):
    assert current_plan["task_count"] == 38
    assert current_plan["counts"] == {
        "in-progress": 8, "proposed": 12, "deferred": 16, "done": 2,
    }
    database = "skybuild_pilot"
    with psycopg.connect(fresh_store.dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    try:
        live_dsn = make_conninfo(fresh_store.dsn, dbname=database)
        store = Store(live_dsn, database, postgres_system_identifier(live_dsn))
        store.migrate()
        token = uuid4().hex + uuid4().hex
        store.provision_principal("current-cutover-owner", token, is_admin=True)
        dispatcher_token = uuid4().hex + uuid4().hex
        store.provision_principal("cutover-dispatcher", dispatcher_token,
                                  grants={"skybuild": {"cord:send", "cord:read", "cord:handle"}})
        dispatcher = store.authenticate(dispatcher_token)
        owner = store.authenticate(token)
        message = store.send_message(dispatcher, "skybuild", {
            "recipient": "current-cutover-owner", "subject": "Existing Cord message",
            "body": "Cutover must preserve unrelated Cord history", "category": "pilot-preflight",
        }, "pre-cutover-message")
        receipt = store.message_action(owner, "skybuild", message["message_id"], "receipt", {},
                                       "pre-cutover-receipt")
        result = cutover_live(store, current_source, CURRENT_CONTRACT, current_plan["import_sha256"], repository=ROOT)
        assert result == {"result": "imported", "project_id": "skybuild",
                          "task_count": 38, "authority": "api"}
        assert cutover_live(store, current_source, CURRENT_CONTRACT, current_plan["import_sha256"],
                            repository=ROOT)["result"] == "unchanged"
        assert store.inbox(owner, "skybuild") == [receipt]
        assert store.message_action(owner, "skybuild", message["message_id"], "receipt", {},
                                    "pre-cutover-receipt") == receipt
        with TestClient(create_app(store)) as client:
            headers = {"Authorization": "Bearer " + token}
            tasks = client.get("/api/v1/projects/skybuild/tasks", headers=headers)
            assert tasks.status_code == 200 and len(tasks.json()) == 38
            expected = {record["task_id"] for record in current_plan["records"]}
            assert {task["task_id"] for task in tasks.json()} == expected
            cutover_task = client.get("/api/v1/projects/skybuild/tasks/SKYBUILD-TASK-CUTOVER", headers=headers)
            assert cutover_task.status_code == 200
            assert cutover_task.json()["next_action"].startswith("freeze all three current ledgers in a clean commit")
            history = client.get("/api/v1/projects/skybuild/tasks/SKYBUILD-TASK-CUTOVER/history", headers=headers)
            assert history.status_code == 200 and len(history.json()) == 1
            assert history.json()[0]["operation"] == "imported"
            updated = client.patch(
                "/api/v1/projects/skybuild/tasks/SKYBUILD-TASK-CUTOVER",
                headers={**headers, "If-Match": "1", "Idempotency-Key": "cutover-verified"},
                json={"next_action": "retire mastertodo.md as editable authority and retain a generated read-only export"},
            )
            assert updated.status_code == 200 and updated.json()["revision"] == 2
            assert updated.json()["next_action"].startswith("retire mastertodo.md")
            revised_history = client.get("/api/v1/projects/skybuild/tasks/SKYBUILD-TASK-CUTOVER/history", headers=headers)
            assert [event["operation"] for event in revised_history.json()] == ["imported", "updated"]
        with store._connection() as connection:
            receipt = connection.execute("SELECT authority, task_count, status_counts FROM ledger_imports WHERE project_id = 'skybuild'").fetchone()
            assert receipt["authority"] == "api" and receipt["task_count"] == 38
            assert receipt["status_counts"] == current_plan["counts"]
    finally:
        with psycopg.connect(fresh_store.dsn, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))


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
    assert apply(store, plan)["result"] == "unchanged"
    with store._connection() as connection:
        assert connection.execute("SELECT count(*) AS count FROM messages").fetchone()["count"] == 1


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


@pytest.mark.parametrize("column,value", [("task_count", 27), ("status_counts", {"proposed": 28})])
def test_replay_refuses_changed_receipt_counts(plan, fresh_store, column, value):
    apply(fresh_store, plan)
    with fresh_store._connection() as connection:
        connection.execute(sql.SQL("UPDATE ledger_imports SET {} = %s").format(sql.Identifier(column)),
                           (json.dumps(value) if column == "status_counts" else value,))
    with pytest.raises(DomainError, match="different import"):
        apply(fresh_store, plan)


def test_replay_refuses_an_extra_import_receipt(plan, fresh_store):
    apply(fresh_store, plan)
    with fresh_store._connection() as connection:
        connection.execute("INSERT INTO ledger_imports (project_id, commit_id, content_sha256, import_sha256, task_count, status_counts, authority) "
                           "SELECT 'other', commit_id, content_sha256, import_sha256, task_count, status_counts, authority "
                           "FROM ledger_imports WHERE project_id = 'skybuild'")
    with pytest.raises(DomainError, match="multiple import receipts"):
        apply(fresh_store, plan)


def test_replay_refuses_lineage_not_in_frozen_import(plan, fresh_store):
    apply(fresh_store, plan)
    source, target = (record["task_id"] for record in plan["records"][:2])
    with fresh_store._connection() as connection:
        connection.execute("INSERT INTO task_lineage (event_id, project_id, source_task_id, target_task_id, action) "
                           "VALUES (%s, 'skybuild', %s, %s, 'split')", (uuid4(), source, target))
    with pytest.raises(DomainError, match="destination changed"):
        apply(fresh_store, plan)


def test_import_waits_for_authorized_writer_then_refuses_conflicting_receipt(plan, fresh_store, monkeypatch):
    # This is an API-owned project; the explicit receipt authorizes its writer
    # and gives the importer a row to fence before it rejects the foreign receipt.
    with fresh_store._connection() as connection:
        connection.execute(
            "INSERT INTO ledger_imports (project_id, commit_id, content_sha256, import_sha256, "
            "task_count, status_counts, authority) VALUES ('skybuild', 'api', %s, %s, 0, '{}'::jsonb, 'api')",
            ("0" * 64, "1" * 64),
        )
    token = uuid4().hex + uuid4().hex
    fresh_store.provision_principal("concurrent-writer", token, is_admin=True)
    writer = fresh_store.authenticate(token)
    authorized, release = Event(), Event()
    original = fresh_store._authorize

    def pause_after_authority_read(connection, principal, project_id, operation):
        result = original(connection, principal, project_id, operation)
        authorized.set()
        if not release.wait(4):
            raise AssertionError("Writer was not released")
        return result

    monkeypatch.setattr(fresh_store, "_authorize", pause_after_authority_read)
    with ThreadPoolExecutor(max_workers=2) as pool:
        writer_future = pool.submit(fresh_store.create_task, writer, "skybuild",
                                    {"task_id": "concurrent", "title": "Concurrent", "description": "Brief"}, "writer-key")
        assert authorized.wait(2)
        import_future = pool.submit(apply, fresh_store, plan)
        try:
            deadline = time.monotonic() + 3
            waiting = False
            while time.monotonic() < deadline:
                with psycopg.connect(fresh_store.dsn) as connection:
                    waiting = connection.execute(
                        "SELECT EXISTS (SELECT 1 FROM pg_locks l JOIN pg_class c ON c.oid = l.relation "
                        "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'skybuild' "
                        "AND c.relname = 'ledger_imports' AND l.mode = 'AccessExclusiveLock' AND NOT l.granted)"
                    ).fetchone()[0]
                if waiting:
                    break
                time.sleep(0.02)
            assert waiting, "Importer did not wait on the authority receipt"
        finally:
            release.set()
        assert writer_future.result(timeout=5)["task_id"] == "concurrent"
        with pytest.raises(DomainError, match="different import"):
            import_future.result(timeout=5)
