"""Operator producer checks real Git objects; only GitHub observations are faked."""
from copy import deepcopy
from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from skybuild.manual_integration import DEFAULT_GATE_COMMAND_SHA256, digest
from skybuild.workflow import Place
from test_manual_integration import token

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("manual_receipt_operator", ROOT / "scripts/manual_integration_receipt.py")
operator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(operator)


def run(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def task_view(token):
    return {"token": token.to_dict(), "task": {"project_id": token.project_id, "task_id": token.task_id,
        "title": "Bounded change", "priority": 0, "dependencies": [], "responsible": token.responsible,
        "next_action": "Verify publication", "blocker": None, "revision": token.revision, "status": "in-progress",
        "acceptance_criteria": ["The exact head is published"], "metadata": {"_skybuild_workflow": {
            "generation": 1, "petri": {"schema_version": 1, "token": token.to_dict()}}}}}


@pytest.fixture
def git_case(tmp_path, token):
    repo, remote = tmp_path / "repo", tmp_path / "remote.git"
    repo.mkdir()
    run(repo, "init", "-b", "dev-005")
    run(repo, "config", "user.name", "Test operator")
    run(repo, "config", "user.email", "operator@localhost")
    (repo / "README.md").write_text("Initial README\n")
    run(repo, "add", "README.md")
    run(repo, "commit", "-m", "base")
    base = run(repo, "rev-parse", "HEAD")
    run(repo, "init", "--bare", str(remote))
    run(repo, "remote", "add", "origin", str(remote))
    run(repo, "push", "origin", "dev-005")
    run(repo, "switch", "-c", "task/smoke")
    (repo / "README.md").write_text("Initial README\nmodified on 2026-10-09T21:00:00-05:00\n")
    run(repo, "commit", "-am", "bounded change")
    head = run(repo, "rev-parse", "HEAD")
    run(repo, "push", "origin", "task/smoke")
    run(repo, "switch", "dev-005")
    run(repo, "merge", "--no-ff", "-m", "prepared bundle", head)
    candidate = run(repo, "rev-parse", "HEAD")
    tree = run(repo, "rev-parse", "HEAD^{tree}")
    run(repo, "branch", "bundle/smoke", candidate)
    prepared = tmp_path / "prepared"
    prepared.mkdir()
    review, policy = tmp_path / "review.md", tmp_path / "policy.md"
    review.write_text("Independent exact-head review passed: " + head)
    policy.write_text("Required normal project gate and separate review")
    inputs = {"schema": "skybuild.bundle-input.v1", "checkout": str(repo),
        "target": {"ref": "refs/heads/dev-005", "sha": base},
        "policy": {"path": str(policy), "sha256": operator.file_digest(policy)},
        "members": [{"task_id": token.task_id, "ref": "refs/heads/task/smoke", "sha": head,
            "reviewer": "independent-reviewer", "review": {"path": str(review), "sha256": operator.file_digest(review)}}]}
    import hashlib
    fingerprint = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    owner = {"inputs": inputs, "fingerprint": fingerprint}
    (prepared / "inputs.json").write_text(json.dumps(owner))
    report = {"schema": "skybuild.bundle-preparation.v1", "ok": True, **owner,
        "candidate_head": candidate, "candidate_tree": tree, "included": [{"task_id": token.task_id, "head": head}]}
    (prepared / "report.json").write_text(json.dumps(report))
    values = {"source_head": head, "target_base": base}
    current = replace(token, **values, evidence=tuple(replace(item, **values) for item in token.evidence))
    frozen = operator.freeze_packet(repo, task_view(current), "owner", "freeze-real", prepared)
    run(repo, "switch", "--detach", base)
    run(repo, "merge", "--no-ff", "-m", "tested candidate", candidate)
    tested = run(repo, "rev-parse", "HEAD")
    run(repo, "switch", "--detach", base)
    run(repo, "merge", "--no-ff", "-m", "published candidate", candidate)
    published = run(repo, "rev-parse", "HEAD")
    run(repo, "push", "origin", "HEAD:refs/heads/dev-005")
    gate_path = tmp_path / "gate.json"
    gate = {"schema": "skybuild.gate-run.v1", "ok": True, "run_id": "pr-1-real",
        "head": tested, "tree": tree, "command_sha256": DEFAULT_GATE_COMMAND_SHA256,
        "phase": "terminal", "status": "passed", "cleanup": "confirmed", "exit_code": 0}
    gate_path.write_text(json.dumps(gate))
    integrated = {"ok": True, "merged": True, "pr": 1, "head": candidate, "base": base,
        "expected_base": base, "candidate_head": tested, "candidate_tree": tree,
        "atomic_expected_base": False, "published_commit": published, "gate_artifact": str(gate_path),
        "gate": {"ok": True, "cleaned_up": True, "run_id": gate["run_id"], "artifact": str(gate_path)}}
    integration_path = tmp_path / "integration.json"
    integration_path.write_text(json.dumps(integrated))
    integrating = replace(current, place=Place.INTEGRATING, revision=current.revision + 1,
                          bundle_id=frozen["bundle"]["bundle_id"])
    return {"repo": repo, "prepared": prepared, "review": review, "view": task_view(integrating), "frozen": frozen,
        "integration_path": integration_path, "integrated": integrated, "gate_path": gate_path, "gate": gate,
        "observed": {"state": "MERGED", "headRefOid": candidate, "baseRefName": "dev-005", "mergeCommit": {"oid": published}}}


def fake_github(monkeypatch, case):
    original = operator.command
    def command(checkout, *argv):
        if argv[0] == "gh":
            return json.dumps(case["observed"])
        return original(checkout, *argv)
    monkeypatch.setattr(operator, "command", command)


def test_real_git_prepare_gate_tree_and_published_inclusion(git_case, monkeypatch):
    case = git_case
    fake_github(monkeypatch, case)
    packet = operator.accepted_packet(case["repo"], case["view"], "owner", "accept-real", case["frozen"], case["integration_path"])
    assert packet["freeze_sha256"] == digest(case["frozen"])
    assert packet["gate"]["artifact_sha256"] == operator.file_digest(case["gate_path"])
    assert packet["publication"]["target_tree"] == case["frozen"]["bundle"]["candidate_tree"]


@pytest.mark.parametrize("fault", ["cleanup", "custom-command", "gate-tree", "tested-object", "pr-head", "pr-base", "published-tree", "unknown"])
def test_operator_does_not_emit_acceptance_for_bad_external_evidence(git_case, monkeypatch, fault):
    case = git_case
    fake_github(monkeypatch, case)
    if fault == "cleanup":
        case["gate"]["cleanup"] = "unknown"
    elif fault == "custom-command":
        case["gate"]["command_sha256"] = "0" * 64
    elif fault == "gate-tree":
        case["gate"]["tree"] = "0" * 40
    elif fault == "tested-object":
        case["gate"]["head"] = case["integrated"]["candidate_head"] = case["frozen"]["bundle"]["base_commit"]
    elif fault == "pr-head":
        case["observed"]["headRefOid"] = "0" * 40
    elif fault == "pr-base":
        case["observed"]["baseRefName"] = "main"
    elif fault == "published-tree":
        bad = case["frozen"]["bundle"]["base_commit"]
        case["observed"]["mergeCommit"]["oid"] = case["integrated"]["published_commit"] = bad
    else:
        case["integrated"]["merged"] = False
    case["gate_path"].write_text(json.dumps(case["gate"]))
    case["integration_path"].write_text(json.dumps(case["integrated"]))
    with pytest.raises(operator.ReceiptError):
        operator.accepted_packet(case["repo"], case["view"], "owner", "accept-real", case["frozen"], case["integration_path"])


def test_preparation_rechecks_review_bytes_and_complete_membership(git_case):
    case = git_case
    case["review"].write_text("Changed evidence")
    with pytest.raises(operator.ReceiptError):
        operator.prepared_bundle(case["repo"], case["prepared"])


@pytest.mark.parametrize("fault", ["too-many-members", "option-ref", "option-head"])
def test_preparation_bounds_inputs_before_git_commands(git_case, monkeypatch, fault):
    case = git_case
    owner = operator.read_json(case["prepared"] / "inputs.json")
    if fault == "too-many-members":
        owner["inputs"]["members"] *= 21
    elif fault == "option-ref":
        owner["inputs"]["members"][0]["ref"] = "--upload-pack=untrusted"
    else:
        owner["inputs"]["members"][0]["sha"] = "--help"
    import hashlib
    owner["fingerprint"] = hashlib.sha256(json.dumps(owner["inputs"], sort_keys=True).encode()).hexdigest()
    report = operator.read_json(case["prepared"] / "report.json")
    report.update(owner)
    (case["prepared"] / "inputs.json").write_text(json.dumps(owner))
    (case["prepared"] / "report.json").write_text(json.dumps(report))
    monkeypatch.setattr(operator, "command", lambda *args: pytest.fail("Malformed inputs reached Git"))
    with pytest.raises(operator.ReceiptError):
        operator.prepared_bundle(case["repo"], case["prepared"])


@pytest.mark.parametrize("kind", ["symlink", "fifo", "oversize"])
def test_artifact_reader_rejects_special_or_unbounded_files(tmp_path, kind):
    path = tmp_path / "artifact"
    if kind == "symlink":
        source = tmp_path / "source"
        source.write_text("{}")
        path.symlink_to(source)
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        path.write_bytes(b"x" * 262145)
    with pytest.raises((OSError, operator.ReceiptError)):
        operator.read_json(path)


def test_private_packet_creation_is_exclusive_and_preserves_original(tmp_path):
    tmp_path.chmod(0o700)
    path = tmp_path / "packet.json"
    operator.write_new(path, {"operation_id": "original"})
    with pytest.raises(FileExistsError):
        operator.write_new(path, {"operation_id": "replacement"})
    assert operator.read_json(path) == {"operation_id": "original"}
    assert path.stat().st_mode & 0o777 == 0o600
