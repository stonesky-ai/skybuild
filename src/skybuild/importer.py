"""Frozen, one-shot Markdown ledger import. This module never switches authority."""

from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import subprocess
from uuid import uuid4

from psycopg import sql
from psycopg.types.json import Jsonb

from .contracts import DomainError, valid_identifier
from .ledger import build_manifest
from .store import Store, _public

LEDGERS = ("mastertodo.md", "deferred.md", "alreadydone.md")
_TITLE = re.compile(r"^## (SKYBUILD-[A-Z0-9-]+)\s+[—–-]\s+(.+?)\s*$")
_ACCEPTANCE = re.compile(r"^- Acceptance: (.+)$", re.MULTILINE)


def _refuse(message: str) -> None:
    raise DomainError("import_contract", message, 409)


def prepare_import(ledger_dir: Path, contract_path: Path) -> dict:
    """Validate exact committed source bytes and return a reviewable import plan."""
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    if set(contract) != {"schema_version", "commit", "content_sha256", "sources", "dependencies"} or contract["schema_version"] != 1:
        _refuse("Unknown frozen import contract")
    commit = contract["commit"]
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        _refuse("Frozen import requires a full commit ID")
    paths = [ledger_dir / name for name in LEDGERS]
    manifest = build_manifest(paths)
    if manifest["sources"] != contract["sources"] or manifest["content_sha256"] != contract["content_sha256"]:
        _refuse("Ledger bytes differ from frozen contract")
    repo = ledger_dir.parent.parent
    for path in paths:
        result = subprocess.run(
            ["git", "show", f"{commit}:docs/design/{path.name}"], cwd=repo,
            capture_output=True, check=False,
        )
        if result.returncode or result.stdout != path.read_bytes():
            _refuse(f"{path.name} differs from frozen Git commit")
    mapping = contract["dependencies"]
    ids = set(manifest["task_ids"])
    if not isinstance(mapping, dict) or set(mapping) != ids:
        _refuse("Dependency mapping must cover every task exactly")
    records = []
    for order, task in enumerate(manifest["tasks"]):
        task_id, raw = task["task_id"], task["raw"]
        dependencies = mapping[task_id]
        if not isinstance(dependencies, list) or len(dependencies) != len(set(dependencies)) or any(dep not in ids or dep == task_id for dep in dependencies):
            _refuse(f"Invalid explicit dependencies for {task_id}")
        heading = _TITLE.match(raw.splitlines()[0])
        if not heading or heading.group(1) != task_id:
            _refuse(f"Invalid task heading for {task_id}")
        acceptance = _ACCEPTANCE.findall(raw)
        architecture = re.findall(r"^- Architecture: (.+)$", raw, re.MULTILINE)
        record = {
            "task_id": task_id,
            "title": heading.group(2),
            "description": raw,
            "status": task["status"],
            "priority": order,
            "dependencies": sorted(dependencies),
            "acceptance_criteria": acceptance,
            "architecture_refs": architecture,
            "phase": "imported",
            "next_action": None if task["status"] == "done" else "Review imported ledger task before execution",
            "responsible": "owner",
            "metadata": {"ledger_import": {
                "commit": commit,
                "manifest_sha256": manifest["content_sha256"],
                "source": task["source"],
                "source_sha256": next(source["sha256"] for source in manifest["sources"] if source["name"] == task["source"]),
                "raw_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                "source_order": order,
                "status_text": task["status_text"],
            }},
        }
        # Apply the same bounds as ordinary task creation before touching PostgreSQL.
        Store._task_values({key: value for key, value in record.items() if key != "task_id"})
        records.append(record)
    graph = {record["task_id"]: record["dependencies"] for record in records}
    def visit(task_id: str, trail: set[str]) -> None:
        if task_id in trail:
            _refuse("Explicit dependency mapping contains a cycle")
        for dependency in graph[task_id]:
            visit(dependency, trail | {task_id})
    for task_id in graph:
        visit(task_id, set())
    import_sha256 = hashlib.sha256(json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"commit": commit, "content_sha256": manifest["content_sha256"], "import_sha256": import_sha256,
            "sources": manifest["sources"], "counts": dict(Counter(record["status"] for record in records)),
            "task_count": len(records), "records": records, "authority": "markdown"}


def import_frozen(store: Store, project_id: str, ledger_dir: Path, contract_path: Path, expected_import_sha256: str) -> dict:
    """Atomically import into an empty destination; replay verifies unchanged rows."""
    plan = prepare_import(ledger_dir, contract_path)
    if plan["import_sha256"] != expected_import_sha256:
        _refuse("Frozen import plan hash differs from reviewed hash")
    if not valid_identifier(project_id):
        _refuse("Invalid project ID")
    store.readiness()
    with store._connection() as connection:
        connection.execute("SELECT pg_advisory_xact_lock(hashtextextended('skybuild:frozen-import', 0))")
        connection.execute("LOCK TABLE tasks, task_dependencies, task_journal, task_lineage, messages, cord_journal, idempotency, ledger_imports IN ACCESS EXCLUSIVE MODE")
        receipts = connection.execute("SELECT * FROM ledger_imports LIMIT 2").fetchall()
        if len(receipts) > 1:
            _refuse("Destination has multiple import receipts")
        receipt = receipts[0] if receipts else None
        if receipt:
            if (receipt["project_id"] != project_id or receipt["content_sha256"] != plan["content_sha256"]
                    or receipt["import_sha256"] != plan["import_sha256"] or receipt["commit_id"] != plan["commit"]
                    or receipt["task_count"] != plan["task_count"] or receipt["status_counts"] != plan["counts"]
                    or receipt["authority"] != "markdown"):
                _refuse("Destination already has a different import")
            rows = connection.execute("SELECT project_id, task_id, title, description, status, priority, acceptance_criteria, architecture_refs, phase, next_action, responsible, metadata, revision FROM tasks ORDER BY priority").fetchall()
            if len(rows) != plan["task_count"] or connection.execute("SELECT count(*) AS count FROM task_journal").fetchone()["count"] != len(rows):
                _refuse("Imported destination changed")
            for table in ("task_lineage", "messages", "cord_journal", "idempotency"):
                if connection.execute(sql.SQL("SELECT 1 FROM {} LIMIT 1").format(sql.Identifier(table))).fetchone():
                    _refuse("Imported destination changed")
            for row, expected in zip(rows, plan["records"]):
                actual = _public(row)
                for field in ("task_id", "title", "description", "status", "priority", "acceptance_criteria", "architecture_refs", "phase", "next_action", "responsible", "metadata"):
                    if actual[field] != expected[field]:
                        _refuse("Imported destination changed")
                if actual["project_id"] != project_id or actual["revision"] != 1:
                    _refuse("Imported destination changed")
                dependencies = connection.execute("SELECT dependency_id FROM task_dependencies WHERE project_id = %s AND task_id = %s ORDER BY dependency_id", (project_id, row["task_id"])).fetchall()
                if [item["dependency_id"] for item in dependencies] != expected["dependencies"]:
                    _refuse("Imported dependencies changed")
                journal = connection.execute("SELECT actor, operation, revision, reason, before_state, after_state FROM task_journal WHERE project_id = %s AND task_id = %s", (project_id, row["task_id"])).fetchone()
                if (not journal or journal["actor"] != "skybuild-ledger-import" or journal["operation"] != "imported"
                        or journal["revision"] != 1 or journal["reason"] != f"Frozen ledger import {plan['commit']}"
                        or journal["before_state"] is not None or journal["after_state"] != store._task(connection, project_id, row["task_id"])):
                    _refuse("Imported history changed")
            return {"result": "unchanged", "project_id": project_id, "task_count": len(rows), "authority": "markdown"}
        for table in ("tasks", "task_dependencies", "task_journal", "task_lineage", "messages", "cord_journal", "idempotency"):
            if connection.execute(sql.SQL("SELECT 1 FROM {} LIMIT 1").format(sql.Identifier(table))).fetchone():
                _refuse("Destination contains unrelated data")
        actor = "skybuild-ledger-import"
        if connection.execute("SELECT 1 FROM principals WHERE principal_id = %s", (actor,)).fetchone():
            _refuse("Reserved import actor already exists")
        connection.execute("INSERT INTO principals (principal_id, token_verifier, is_admin) VALUES (%s, %s, false)",
                           (actor, hashlib.sha256(uuid4().bytes).hexdigest()))
        for record in plan["records"]:
            connection.execute(
                "INSERT INTO tasks (project_id, task_id, title, description, status, priority, acceptance_criteria, architecture_refs, phase, next_action, responsible, metadata) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (project_id, record["task_id"], record["title"], record["description"], record["status"],
                 record["priority"], record["acceptance_criteria"], record["architecture_refs"],
                 record["phase"], record["next_action"], record["responsible"], Jsonb(record["metadata"])),
            )
        for record in plan["records"]:
            for dependency in record["dependencies"]:
                connection.execute("INSERT INTO task_dependencies VALUES (%s, %s, %s)", (project_id, record["task_id"], dependency))
            after = store._task(connection, project_id, record["task_id"])
            connection.execute(
                "INSERT INTO task_journal (event_id, project_id, task_id, actor, operation, revision, reason, after_state) "
                "VALUES (%s, %s, %s, %s, 'imported', 1, %s, %s)",
                (uuid4(), project_id, record["task_id"], actor, f"Frozen ledger import {plan['commit']}", Jsonb(after)),
            )
        connection.execute("INSERT INTO ledger_imports (project_id, commit_id, content_sha256, import_sha256, task_count, status_counts, authority) VALUES (%s, %s, %s, %s, %s, %s, 'markdown')",
                           (project_id, plan["commit"], plan["content_sha256"], plan["import_sha256"], plan["task_count"], Jsonb(plan["counts"])))
        return {"result": "imported", "project_id": project_id, "task_count": plan["task_count"], "authority": "markdown"}
