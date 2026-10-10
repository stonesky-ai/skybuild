"""Conservative enrollment and a usable versioned requirements profile."""
from copy import deepcopy

import pytest

from skybuild.enrollment import DEFAULT_POLICY_VERSION, DEFAULT_REQUIREMENTS, enrollment_token, install_token
from skybuild.workflow import Place, ValidationStage


def legacy(status="proposed", phase="triage", criteria=True):
    return dict(project_id="project", task_id="task", title="Work", description="Scope", priority=2,
                acceptance_criteria=["Verify work"] if criteria else [], status=status, phase=phase,
                revision=1, responsible="owner", next_action="Inspect", blocker=None, metadata={})


@pytest.mark.parametrize("defined,expected", [(True, Place.READY), (False, Place.HOLD)])
def test_new_task_has_current_required_check_profile(defined, expected):
    current = enrollment_token(legacy(criteria=defined), input_generation=1, revision=1, new=True)
    assert current.place == expected
    assert current.policy_version == DEFAULT_POLICY_VERSION
    assert current.requirements == DEFAULT_REQUIREMENTS == tuple(ValidationStage)
    assert (current.project_id, current.task_id, current.revision) == ("project", "task", 1)
    assert bool(current.hold_reason) == (expected == Place.HOLD)


@pytest.mark.parametrize("status,phase", [("in-progress", "working"), ("in-progress", "ready-for-review"),
    ("in-progress", "integrating"), ("done", "done"), ("blocked", "needs-rebase"),
    ("deferred", "deferred"), ("proposed", "unknown")])
def test_legacy_labels_never_invent_execution_or_acceptance(status, phase):
    current = enrollment_token(legacy(status, phase), input_generation=3, revision=2)
    assert current.place == Place.HOLD and current.hold_reason
    assert current.attempt_id is None and current.claim_fence is None and current.bundle_id is None
    assert current.input_generation == 3


@pytest.mark.parametrize("until", ["2090-01-01", "invalid", "2090-01-01T00:00:00"])
def test_malformed_legacy_date_requires_diagnostic_hold(until):
    task = legacy("deferred")
    task["metadata"] = {"_skybuild_workflow": {"deferral": {"until": until}}}
    assert enrollment_token(task, input_generation=1, revision=2).place == Place.HOLD


def test_explicit_deferral_and_supersession_preserve_control():
    task = legacy("deferred")
    task["metadata"] = {"_skybuild_workflow": {"deferral": {
        "until": "2090-01-01T00:00:00+00:00", "reason": "Owner postpones work"}}}
    current = enrollment_token(task, input_generation=1, revision=2)
    assert current.place == Place.DEFERRED and current.hold_reason == "Owner postpones work"
    assert current.deferred_until == "2090-01-01T00:00:00+00:00"
    task["status"] = "superseded"
    retired = enrollment_token(task, input_generation=1, revision=2)
    assert retired.place == Place.HOLD and retired.superseded


def test_enrollment_preserves_snapshot_and_unrelated_metadata():
    task = legacy("ready")
    task["metadata"] = {"link": "https://example.test/task", "_skybuild_workflow": {"old_event": "retained"}}
    original = deepcopy(task)
    current = enrollment_token(task, input_generation=4, revision=2)
    assert task == original
    metadata = deepcopy(task["metadata"])
    install_token(metadata, current)
    assert metadata["link"] == task["metadata"]["link"]
    assert metadata["_skybuild_workflow"]["old_event"] == "retained"
    assert metadata["_skybuild_workflow"]["petri"]["schema_version"] == 1
