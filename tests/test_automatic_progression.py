"""Automatic bundle planning waits for independently recorded current evidence."""

from dataclasses import replace
from pathlib import Path

import pytest

from skybuild import automatic_progression as progression
from skybuild.workflow import Place, ResultState, TaskToken, ValidationResult, ValidationStage


def _token(task_id, worker, reviewer, artifact):
    values = dict(source_head=("a" if task_id == "task-a" else "b") * 40,
                  target_base="c" * 40, attempt_id="attempt-" + task_id,
                  claim_fence=1, input_generation=2, definition_revision=1,
                  policy_version="petri-checks-v1")
    evidence = tuple(ValidationResult("project", task_id, stage, ResultState.PASSED,
                     producer=reviewer if stage == ValidationStage.CODE_REVIEW else "checker",
                     check_id=stage.value,
                     artifacts=(artifact,) if stage == ValidationStage.CODE_REVIEW else (),
                     **values) for stage in ValidationStage)
    return TaskToken("project", task_id, place=Place.VALIDATING,
                     source_branch="refs/heads/task/" + task_id, responsible=worker,
                     revision=10, requirements=tuple(ValidationStage), evidence=evidence, **values)


def _setup(tmp_path, monkeypatch):
    import hashlib
    views, approved = {}, []
    for task_id in ("task-a", "task-b"):
        head = ("a" if task_id == "task-a" else "b") * 40
        path = tmp_path / (task_id + ".review")
        path.write_text("Independent reviewer approves " + head + "\n")
        artifact = str(path) + "#sha256=" + hashlib.sha256(path.read_bytes()).hexdigest()
        worker = "worker-" + task_id
        token = _token(task_id, worker, "reviewer", artifact)
        views[task_id] = {"task": {"id": task_id}, "token": token.to_dict(), "actual": token}
        approved.append({"task_id": task_id, "worker": worker, "branch": "task/" + task_id,
                         "reviewer": "reviewer", "review_path": str(path)})
    monkeypatch.setattr(progression.Store, "workflow_token", lambda task: views[task["id"]]["actual"])
    class Client:
        def task_workflow(self, project, task_id):
            return views[task_id]
    return Client(), views, approved


def test_two_current_independent_reviews_make_one_frozen_manifest(tmp_path, monkeypatch):
    client, views, approved = _setup(tmp_path, monkeypatch)
    members = progression.reviewed_members(client, "project", approved, base_sha="c" * 40)
    assert [member["task_id"] for member in members] == ["task-a", "task-b"]
    assert all(member["workflow"]["claim_fence"] == 1 for member in members)
    policy = tmp_path / "policy.txt"
    policy.write_text("Reviewed policy")
    manifest = progression.bundle_manifest(members, target_ref="refs/heads/dev-006",
                                           base_sha="c" * 40, policy_evidence=policy)
    assert manifest["members"] == members and manifest["schema"] == "skybuild.bundle-input.v1"


def test_waits_for_independent_stage_and_rejects_fence_or_reviewer_drift(tmp_path, monkeypatch):
    client, views, approved = _setup(tmp_path, monkeypatch)
    current = views["task-b"]["actual"]
    views["task-b"]["actual"] = replace(current, evidence=tuple(item for item in current.evidence
                                                     if item.stage != ValidationStage.LONG_TESTS))
    views["task-b"]["token"] = views["task-b"]["actual"].to_dict()
    assert progression.reviewed_members(client, "project", approved, base_sha="c" * 40) is None
    views["task-b"]["actual"] = replace(current, claim_fence=2)
    views["task-b"]["token"] = views["task-b"]["actual"].to_dict()
    assert progression.reviewed_members(client, "project", approved, base_sha="c" * 40) is None
    views["task-b"]["actual"] = current
    views["task-b"]["token"] = current.to_dict()
    approved[1]["reviewer"] = "author"
    with pytest.raises(progression.ProgressionError):
        progression.reviewed_members(client, "project", approved, base_sha="c" * 40)
