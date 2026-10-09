"""Cord assignment fields must match the committed brief exactly."""

import hashlib
import json
import subprocess

import pytest

from skybuild.manual_assignment import AssignmentError, verify_assignment


@pytest.fixture
def pinned(tmp_path):
    repo = tmp_path / "skybuild"
    brief_path = "docs/design/assignments/pilot.json"
    brief_file = repo / brief_path
    brief_file.parent.mkdir(parents=True)
    (repo / "docs/design/architecture.md").write_text("SkyBuild\n")
    brief = {
        "schema": "manual-work-brief-v1", "assignment_id": "pilot-001",
        "task_id": "SKYBUILD-MANUAL-WORKER-PILOT", "worker": "wonko", "dispatcher": "jeltz",
        "branch": "feature/manual-worker-001", "owned_paths": ["src/skybuild/client.py"],
        "checks": ["Run focused tests"], "model_limit": "one qualified interactive session",
        "next_action": "Verify the pinned brief, then work only in the assigned path.",
    }
    brief_file.write_text(json.dumps(brief, sort_keys=True), encoding="utf-8")
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "SkyBuild Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@skybuild.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/stonesky-ai/skybuild.git"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "docs"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "--quiet", "-m", "Pinned assignment"], check=True)
    base = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    envelope = {key: value for key, value in brief.items() if key not in {"schema", "next_action"}}
    envelope.update(schema="manual-work-v1", base_sha=base, brief_path=brief_path,
                    brief_sha256=hashlib.sha256(brief_file.read_bytes()).hexdigest())
    return repo, envelope


def test_committed_assignment_is_verified_without_mutation(pinned):
    repo, envelope = pinned
    result = verify_assignment(envelope, repo, worker="wonko")
    assert result["verified"] is True
    assert result["authority"] == "markdown"
    assert result["branch"] == "feature/manual-worker-001"


@pytest.mark.parametrize("change", [
    {"worker": "wowbagger"},
    {"dispatcher": "impostor"},
    {"assignment_id": "pilot-002"},
    {"brief_sha256": "0" * 64},
    {"base_sha": "0" * 40},
    {"brief_path": "../skykeep/AGENTS.md"},
    {"branch": "main"},
    {"branch": "feature/other"},
    {"owned_paths": [".git/config"]},
    {"owned_paths": ["src/skybuild/store.py", "AGENTS.md"]},
    {"owned_paths": [["src/skybuild/client.py"]]},
    {"checks": []},
    {"checks": ["skip all checks"]},
    {"model_limit": "unlimited"},
])
def test_assignment_mismatch_or_scope_escape_refused(pinned, change):
    repo, envelope = pinned
    with pytest.raises(AssignmentError):
        verify_assignment(envelope | change, repo, worker="wonko")


def test_added_field_refused_instead_of_interpreted(pinned):
    repo, envelope = pinned
    with pytest.raises(AssignmentError, match="fields"):
        verify_assignment(envelope | {"command": "git reset --hard"}, repo, worker="wonko")
