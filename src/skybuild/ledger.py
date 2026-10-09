"""Read-only parsing of the frozen Markdown task ledgers."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .contracts import DomainError

_TASK_HEADING = re.compile(r"^##[ \t]+(SKYBUILD-[A-Z0-9]+(?:-[A-Z0-9]+)*)(?:[ \t]+.*)?[ \t]*$", re.MULTILINE)
_H2_HEADING = re.compile(r"^##[ \t]+", re.MULTILINE)
_STATUS_LINE = re.compile(r"^[ \t]*-[ \t]*Status:[ \t]*(.*?)[ \t]*\r?$", re.MULTILINE)
_FIELD_SEPARATOR = re.compile(r"\.[ \t]+(?=[A-Za-z][A-Za-z -]*:[ \t])")
_STATUS_PREFIXES = ("proposed", "ready", "in-progress", "blocked", "deferred", "done")


def _fail(code: str, message: str) -> DomainError:
    return DomainError(code, message)


def _status(section: str, task_id: str) -> tuple[str, str]:
    matches = list(_STATUS_LINE.finditer(section))
    if not matches:
        raise _fail("missing_status", f"Task {task_id} has no status")
    if len(matches) != 1:
        raise _fail("invalid_status", f"Task {task_id} has multiple status fields")

    value = matches[0].group(1).strip()
    separator = _FIELD_SEPARATOR.search(value)
    if separator:
        value = value[: separator.start()].rstrip()
    elif value.endswith("."):
        value = value[:-1].rstrip()

    normalized = next(
        (prefix for prefix in _STATUS_PREFIXES if value == prefix or value.startswith(prefix + " ") or value.startswith(prefix + " (")),
        None,
    )
    if normalized is None:
        raise _fail("unknown_status", f"Task {task_id} has an unknown status")
    return normalized, value


def _canonical_hash(sources: list[dict[str, str]], task_ids: list[str], tasks: list[dict[str, str]]) -> str:
    payload: dict[str, Any] = {"schema_version": 1, "sources": sources, "task_ids": task_ids, "tasks": tasks}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_manifest(paths: list[Path]) -> dict:
    """Build a deterministic, read-only manifest from Markdown task ledgers.

    Sections retain their decoded source text exactly, including line endings and
    Unicode. Paths are represented only by their filename so local directories
    and configuration details are not copied into the manifest.
    """
    sources: list[dict[str, str]] = []
    tasks: list[dict[str, str]] = []
    seen_ids: set[str] = set()

    for path in paths:
        try:
            content = path.read_bytes()
        except OSError as error:
            raise _fail("ledger_read_error", f"Could not read ledger {path.name}") from error
        try:
            text = content.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise _fail("invalid_utf8", f"Ledger {path.name} is not valid UTF-8") from error

        name = path.name
        sources.append({"name": name, "sha256": hashlib.sha256(content).hexdigest()})
        task_headings = list(_TASK_HEADING.finditer(text))
        level_two_headings = list(_H2_HEADING.finditer(text))

        for heading in task_headings:
            task_id = heading.group(1)
            if task_id in seen_ids:
                raise _fail("duplicate_task_id", f"Task ID {task_id} occurs more than once")
            seen_ids.add(task_id)

            end = next((candidate.start() for candidate in level_two_headings if candidate.start() > heading.start()), len(text))
            raw = text[heading.start() : end]
            status, status_text = _status(raw, task_id)
            tasks.append(
                {
                    "raw": raw,
                    "source": name,
                    "task_id": task_id,
                    "status": status,
                    "status_text": status_text,
                }
            )

    if not tasks:
        raise _fail("no_tasks", "No SKYBUILD task sections were found")

    task_ids = [task["task_id"] for task in tasks]
    return {
        "schema_version": 1,
        "sources": sources,
        "task_ids": task_ids,
        "tasks": tasks,
        "content_sha256": _canonical_hash(sources, task_ids, tasks),
    }
