from pathlib import Path

import pytest

from skybuild.contracts import DomainError
from skybuild.ledger import build_manifest


def write_ledger(path: Path, task_id: str, status_line: str, body: str = "") -> None:
    path.write_text(f"# Ledger\n\n## {task_id} — Title\n\n- Status: {status_line}\n{body}", encoding="utf-8", newline="")


def test_retains_complete_unicode_multiline_section_and_original_status(tmp_path: Path) -> None:
    path = tmp_path / "mastertodo.md"
    raw = "## SKYBUILD-UNICODE — Über task\r\n\r\n- Status: in-progress (planning only). Area: build.\r\n- Dependencies: SKYBUILD-BASE.\r\n- Brief: 第一行\r\n  第二行\r\n"
    path.write_bytes(("# Ledger\r\n\r\n" + raw + "## SKYBUILD-NEXT — Next\r\n\r\n- Status: ready. Area: build.\r\n").encode("utf-8"))

    manifest = build_manifest([path])

    assert manifest["tasks"][0] == {
        "raw": raw,
        "source": "mastertodo.md",
        "task_id": "SKYBUILD-UNICODE",
        "status": "in-progress",
        "status_text": "in-progress (planning only)",
    }
    assert manifest["task_ids"] == ["SKYBUILD-UNICODE", "SKYBUILD-NEXT"]


def test_canonical_hash_is_deterministic_and_changes_with_content(tmp_path: Path) -> None:
    path = tmp_path / "mastertodo.md"
    write_ledger(path, "SKYBUILD-HASH", "proposed", "- Brief: one\n")

    first = build_manifest([path])
    second = build_manifest([path])
    assert first["content_sha256"] == second["content_sha256"]

    write_ledger(path, "SKYBUILD-HASH", "proposed", "- Brief: two\n")
    changed = build_manifest([path])
    assert changed["content_sha256"] != first["content_sha256"]


def test_rejects_duplicate_task_ids_across_sources(tmp_path: Path) -> None:
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    write_ledger(first, "SKYBUILD-DUPLICATE", "ready")
    write_ledger(second, "SKYBUILD-DUPLICATE", "done")

    with pytest.raises(DomainError) as error:
        build_manifest([first, second])

    assert error.value.code == "duplicate_task_id"


@pytest.mark.parametrize(
    ("status_line", "error_code"),
    [("mystery", "unknown_status"), ("", "missing_status")],
)
def test_rejects_unknown_or_missing_status(tmp_path: Path, status_line: str, error_code: str) -> None:
    path = tmp_path / "ledger.md"
    path.write_text(f"# Ledger\n\n## SKYBUILD-STATUS — Status test\n\n{('- Status: ' + status_line) if status_line else '- Area: build'}\n", encoding="utf-8")

    with pytest.raises(DomainError) as error:
        build_manifest([path])

    assert error.value.code == error_code


def test_rejects_invalid_utf8_and_ledgers_without_tasks(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.md"
    invalid.write_bytes(b"## SKYBUILD-INVALID\n\xff")
    with pytest.raises(DomainError) as error:
        build_manifest([invalid])
    assert error.value.code == "invalid_utf8"

    empty = tmp_path / "empty.md"
    empty.write_text("# Ledger\n\nNothing here.\n", encoding="utf-8")
    with pytest.raises(DomainError) as error:
        build_manifest([empty])
    assert error.value.code == "no_tasks"


def test_reads_the_three_current_ledgers_without_assuming_task_counts() -> None:
    root = Path(__file__).parents[1] / "docs" / "design"
    paths = [root / "mastertodo.md", root / "deferred.md", root / "alreadydone.md"]

    manifest = build_manifest(paths)

    assert manifest["schema_version"] == 1
    assert len(manifest["tasks"]) == len(manifest["task_ids"]) > 0
    assert [source["name"] for source in manifest["sources"]] == [path.name for path in paths]
    assert all(task["source"] in {path.name for path in paths} for task in manifest["tasks"])
