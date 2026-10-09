"""Read-only source reconciliation; never authorizes a task authority switch."""

from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory

from .contracts import DomainError
from .importer import LEDGERS, prepare_import
from .ledger import build_manifest

_DEPENDENCIES = re.compile(r"(?:^|[.;][ \t]+)Dependencies:[ \t]*")
_NEXT_FIELD = re.compile(r"[.;][ \t]+(?=[A-Z][A-Za-z -]*:[ \t]*)")
_TASK_ID = re.compile(r"SKYBUILD-[A-Z0-9]+(?:-[A-Z0-9]+)*")


def _dependency_evidence(task: dict, current_ids: set[str]) -> dict:
    """Expose literal source evidence without converting prose into graph edges."""
    lines = []
    values = []
    for line in task["raw"].splitlines():
        if not line.lstrip().startswith("- "):
            continue
        match = _DEPENDENCIES.search(line.lstrip()[2:])
        if match:
            lines.append(line)
            value = line.lstrip()[2:][match.end():]
            next_field = _NEXT_FIELD.search(value)
            values.append(value[:next_field.start()] if next_field else value)

    mentions = [match.group() for value in values for match in _TASK_ID.finditer(value)]
    resolved = sorted(set(mentions) & current_ids)
    unknown = sorted(set(mentions) - current_ids)
    remainder = []
    for value in values:
        prose = _TASK_ID.sub("", value).strip(" \t,;.")
        if prose and prose.lower() != "none":
            remainder.append(prose)
    counts = Counter(mentions)
    return {
        "dependency_lines": lines,
        "literal_references": resolved,
        "unknown_references": unknown,
        "self_references": [task["task_id"]] if task["task_id"] in mentions else [],
        "duplicate_references": sorted(ref for ref, count in counts.items() if count > 1),
        "unresolved_prose": remainder,
    }


def _fields(task: dict, order: int) -> dict:
    raw = task["raw"]
    heading = re.fullmatch(r"## (SKYBUILD-[A-Z0-9-]+)\s+[—–-]\s+(.+?)\s*", raw.splitlines()[0])
    if not heading:
        raise DomainError("audit_heading", f"Invalid task heading for {task['task_id']}")
    return {
        "title": heading.group(2), "status": task["status"], "status_text": task["status_text"],
        "source": task["source"], "priority": order,
        "acceptance_criteria": re.findall(r"^- Acceptance: (.+)$", raw, re.MULTILINE),
        "architecture_refs": re.findall(r"^- Architecture: (.+)$", raw, re.MULTILINE),
        "raw_sha256": hashlib.sha256(raw.encode()).hexdigest(),
    }


def audit_ledgers(ledger_dir: Path, contract_path: Path) -> dict:
    """Validate frozen projection, then compare current ledger bytes and fields.

    Dependency evidence and workflow defaults require human reconciliation;
    this audit deliberately does not infer or approve destination values.
    """
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    commit = contract.get("commit")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise DomainError("audit_contract", "Frozen audit requires a full commit ID")
    repository = ledger_dir.resolve().parent.parent
    with TemporaryDirectory(prefix="skybuild-ledger-audit-") as directory:
        frozen_dir = Path(directory)
        for name in LEDGERS:
            result = subprocess.run(["git", "show", f"{commit}:docs/design/{name}"],
                                    cwd=repository, capture_output=True, check=False)
            if result.returncode:
                raise DomainError("audit_source", f"Frozen Git blob unavailable for {name}")
            (frozen_dir / name).write_bytes(result.stdout)
        plan = prepare_import(frozen_dir, contract_path, repository=repository)
        frozen = build_manifest([frozen_dir / name for name in LEDGERS])
    current = build_manifest([ledger_dir / name for name in LEDGERS])
    before = {task["task_id"]: _fields(task, order) for order, task in enumerate(frozen["tasks"])}
    after = {task["task_id"]: _fields(task, order) for order, task in enumerate(current["tasks"])}
    current_ids = set(after)
    frozen_ids = set(before)
    frozen_lines = {task["task_id"]: _dependency_evidence(task, current_ids)["dependency_lines"]
                    for task in frozen["tasks"]}
    dependency_reconciliation = []
    for task in current["tasks"]:
        task_id = task["task_id"]
        evidence = _dependency_evidence(task, current_ids)
        if task_id in frozen_ids:
            explicit = sorted(contract["dependencies"][task_id])
            literal = set(evidence["literal_references"])
            evidence["frozen_comparison"] = {
                "explicit_dependencies": explicit,
                "frozen_dependency_lines": frozen_lines[task_id],
                "dependency_lines_changed": evidence["dependency_lines"] != frozen_lines[task_id],
                "literal_only": sorted(literal - set(explicit)),
                "frozen_only": sorted(set(explicit) - literal),
            }
        else:
            evidence["frozen_comparison"] = None
        dependency_reconciliation.append({"task_id": task_id, **evidence})
    changes = []
    for task_id in sorted(before.keys() & after.keys()):
        fields = {field: {"frozen": before[task_id][field], "current": after[task_id][field]}
                  for field in before[task_id] if before[task_id][field] != after[task_id][field]}
        if fields:
            changes.append({"task_id": task_id, "fields": fields})
    stale = frozen["content_sha256"] != current["content_sha256"]
    warnings = ["Audit only: no database comparison, writer fence, authority switch, or cutover acceptance is performed.",
                "Explicit dependency mappings and imported workflow defaults require independent reconciliation before cutover."]
    if stale:
        warnings.insert(0, "STALE FREEZE: current Markdown differs from the reviewed frozen source; do not use the old import plan for cutover. Freeze again and review the new mapping and digest.")
    return {
        "schema_version": 1, "authority": "markdown", "cutover_ready": False,
        "stale_freeze": stale, "frozen_commit": commit, "frozen_import_sha256": plan["import_sha256"],
        "frozen_content_sha256": frozen["content_sha256"], "current_content_sha256": current["content_sha256"],
        "frozen_counts": plan["counts"], "current_counts": dict(Counter(task["status"] for task in current["tasks"])),
        "sources": [{"name": old["name"], "frozen_sha256": old["sha256"], "current_sha256": new["sha256"]}
                    for old, new in zip(frozen["sources"], current["sources"])],
        "added_task_ids": sorted(after.keys() - before.keys()), "removed_task_ids": sorted(before.keys() - after.keys()),
        "changed_tasks": changes, "dependency_reconciliation": dependency_reconciliation,
        "warnings": warnings,
    }
