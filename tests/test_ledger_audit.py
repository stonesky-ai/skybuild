"""Source drift must remain visible without granting cutover authority."""
import json
from pathlib import Path
import subprocess

import pytest

from skybuild.__main__ import main
from skybuild.contracts import DomainError
from skybuild.ledger_audit import audit_ledgers

ROOT = Path(__file__).parents[1]
CONTRACT = ROOT / "docs/design/implementation/frozen_ledger_import.json"
NAMES = ("mastertodo.md", "deferred.md", "alreadydone.md")


@pytest.fixture
def frozen_checkout(tmp_path):
    ledger = tmp_path / "docs/design"
    ledger.mkdir(parents=True)
    # A separate Git directory keeps the fixture read-only with respect to the checkout.
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    subprocess.run(["git", "fetch", "--quiet", str(ROOT), json.loads(CONTRACT.read_text())["commit"]], cwd=tmp_path, check=True)
    for name in NAMES:
        blob = subprocess.run(["git", "show", f"FETCH_HEAD:docs/design/{name}"], cwd=tmp_path, capture_output=True, check=True).stdout
        (ledger / name).write_bytes(blob)
    return ledger


def test_unchanged_source_still_cannot_accept_cutover(frozen_checkout):
    report = audit_ledgers(frozen_checkout, CONTRACT)
    assert report["stale_freeze"] is False
    assert report["changed_tasks"] == report["added_task_ids"] == report["removed_task_ids"] == []
    assert report["authority"] == "markdown"
    assert report["cutover_ready"] is False
    assert report["frozen_import_sha256"] == "1d3400fc5ae31ddfbc9e7e55e3b231b070cf7456e11921728aac6e074cdbcdc0"


def test_detects_normalized_and_raw_drift_without_writing(frozen_checkout):
    path = frozen_checkout / "mastertodo.md"
    content = path.read_text()
    content = content.replace("- Acceptance:", "- Acceptance: Audit changed — ", 1)
    path.write_text(content)
    before = {name: (frozen_checkout / name).read_bytes() for name in NAMES}
    report = audit_ledgers(frozen_checkout, CONTRACT)
    assert report["stale_freeze"] is True
    assert report["warnings"][0].startswith("STALE FREEZE:")
    assert any("acceptance_criteria" in task["fields"] and "raw_sha256" in task["fields"] for task in report["changed_tasks"])
    assert before == {name: (frozen_checkout / name).read_bytes() for name in NAMES}


def test_non_task_prose_drift_is_stale_even_with_identical_fields(frozen_checkout):
    path = frozen_checkout / "mastertodo.md"
    path.write_bytes(b"Audit header\n" + path.read_bytes())
    report = audit_ledgers(frozen_checkout, CONTRACT)
    assert report["stale_freeze"] and not report["changed_tasks"]


def test_contract_tampering_is_refused(frozen_checkout, tmp_path):
    contract = json.loads(CONTRACT.read_text())
    contract["content_sha256"] = "0" * 64
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(contract))
    with pytest.raises(DomainError):
        audit_ledgers(frozen_checkout, path)


def test_cli_reports_stale_freeze_with_distinct_exit(frozen_checkout, capsys, monkeypatch):
    monkeypatch.delenv("SKYBUILD_DSN", raising=False)
    path = frozen_checkout / "mastertodo.md"
    path.write_bytes(b"Drift\n" + path.read_bytes())
    assert main(["ledger-audit", "--ledger-dir", str(frozen_checkout), "--contract", str(CONTRACT)]) == 2
    assert json.loads(capsys.readouterr().out)["authority"] == "markdown"


def test_added_and_removed_ids_are_explicit(frozen_checkout):
    path = frozen_checkout / "mastertodo.md"
    text = path.read_text()
    old_id = "SKYBUILD-TASK-CUTOVER"
    assert old_id in text
    path.write_text(text.replace(old_id, "SKYBUILD-AUDIT-NEW"))
    report = audit_ledgers(frozen_checkout, CONTRACT)
    assert old_id in report["removed_task_ids"]
    assert "SKYBUILD-AUDIT-NEW" in report["added_task_ids"]


def test_duplicate_current_id_is_refused(frozen_checkout):
    path = frozen_checkout / "mastertodo.md"
    path.write_text(path.read_text() + "\n## SKYBUILD-TASK-CUTOVER — Duplicate\n- Status: proposed\n")
    with pytest.raises(DomainError) as error:
        audit_ledgers(frozen_checkout, CONTRACT)
    assert error.value.code == "duplicate_task_id"
