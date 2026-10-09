"""A Cord snapshot cannot expand a worker's committed Git assignment."""

import hashlib
import subprocess
from pathlib import Path

import pytest

from skybuild.manual_assignment import AssignmentError, verify_assignment


ROOT = Path(__file__).parents[1]


def assignment():
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    brief_path = "docs/design/mastertodo.md"
    brief = subprocess.check_output(["git", "show", f"{base}:{brief_path}"], cwd=ROOT)
    return {
        "schema": "manual-work-v1", "assignment_id": "pilot-001",
        "task_id": "SKYBUILD-MANUAL-WORKER-PILOT", "worker": "wonko", "dispatcher": "jeltz",
        "base_sha": base, "brief_path": brief_path, "brief_sha256": hashlib.sha256(brief).hexdigest(),
        "branch": "feature/manual-worker-001", "owned_paths": ["src/skybuild/client.py"],
        "checks": ["Run focused tests"], "model_limit": "one qualified interactive session",
    }


def test_committed_assignment_is_verified_without_mutation():
    result = verify_assignment(assignment(), ROOT, worker="wonko")
    assert result["verified"] is True
    assert result["authority"] == "markdown"
    assert result["branch"] == "feature/manual-worker-001"


@pytest.mark.parametrize("change", [
    {"worker": "wowbagger"},
    {"brief_sha256": "0" * 64},
    {"base_sha": "0" * 40},
    {"brief_path": "../skykeep/AGENTS.md"},
    {"branch": "main"},
    {"owned_paths": [".git/config"]},
    {"owned_paths": [["src/skybuild/client.py"]]},
    {"checks": []},
])
def test_assignment_mismatch_or_scope_escape_refused(change):
    envelope = assignment() | change
    with pytest.raises(AssignmentError):
        verify_assignment(envelope, ROOT, worker="wonko")


def test_added_field_refused_instead_of_interpreted():
    envelope = assignment() | {"command": "git reset --hard"}
    with pytest.raises(AssignmentError, match="fields"):
        verify_assignment(envelope, ROOT, worker="wonko")
