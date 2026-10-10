"""Focused stage inputs bind one actual worker head and a clean Git copy."""

from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from skybuild.automatic_focused import (FocusedError, candidate_checkout,
                                        focused_payload)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], check=True,
                            capture_output=True, text=True)
    return result.stdout.strip()


def test_focused_input_binds_live_worker_tuple_and_exact_stage():
    task = {"task_id": "task-a", "assignment_id": "assignment-a", "worker_id": "worker-a",
            "task_branch": "refs/heads/task/worker-a", "brief_sha256": "b" * 64,
            "definition_revision": 3, "policy_version": "petri-checks-v1"}
    policy = {"project_id": "skybuild", "target_ref": "refs/heads/dev-006",
              "base_sha": "a" * 40}
    token = SimpleNamespace(task_id="task-a", responsible="worker-a",
                            source_branch="refs/heads/task/worker-a",
                            target_base="a" * 40, source_head="c" * 40,
                            attempt_id="attempt-a", claim_fence=2,
                            input_generation=4, definition_revision=3,
                            policy_version="petri-checks-v1")
    result = focused_payload(policy, task, token, stage="unit",
                             permit_sha256="d" * 64,
                             conductor_intent_sha256="e" * 64,
                             source_tree="f" * 40, archive_sha256="1" * 64,
                             history_sha256="2" * 64)
    assert result["head_sha"] == token.source_head
    assert result["workflow"]["claim_fence"] == 2
    assert result["stage"] == "unit"
    token.claim_fence = 3
    token.responsible = "another-worker"
    with pytest.raises(FocusedError):
        focused_payload(policy, task, token, stage="long",
                        permit_sha256="d" * 64,
                        conductor_intent_sha256="e" * 64,
                        source_tree="f" * 40, archive_sha256="1" * 64,
                        history_sha256="2" * 64)


def test_candidate_checkout_is_clean_exact_detached_copy(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q")
    _git(source, "config", "user.name", "Tester")
    _git(source, "config", "user.email", "tester@example.invalid")
    (source / "tracked.txt").write_text("approved\n")
    _git(source, "add", "tracked.txt")
    _git(source, "commit", "-qm", "approved")
    head = _git(source, "rev-parse", "HEAD")
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    copy = candidate_checkout(source, state, head)
    assert _git(copy, "rev-parse", "HEAD") == head
    assert candidate_checkout(source, state, head) == copy
    (copy / "tracked.txt").write_text("changed\n")
    with pytest.raises(FocusedError):
        candidate_checkout(source, state, head)
