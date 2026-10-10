"""One-shot marshall checks use real isolated Git, explicit reviews, and fake REST."""
import json
from contextlib import nullcontext
import os
import subprocess
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import marshall_bundle as marshall
from skybuild.bundling import BundlePlanningError
from test_prepare_bundle import repository, git  # noqa: F401


ADMISSION_DETAILS = [
    ('Available memory is below the required 6 GiB reserve', 'Available memory is below the required 6 GiB reserve'),
    ('Available memory cannot be measured', 'Available memory cannot be measured'),
    ('Remote refs moved or are missing; freeze new inputs', 'Remote refs moved or are missing; freeze new inputs'),
    ('private command stderr: token=must-not-appear', None),
    ('Available memory is below the required 6 GiB reserve\nprivate token', None),
]


class API:
    def __init__(self, ids):
        self.tasks = {task_id: {"task_id": task_id, "status": "ready", "revision": 1,
                               "phase": "ready-for-work", "dependencies": []} for task_id in ids}
        self.reads = []

    def whoami(self):
        return {"principal_id": "owner"}

    def get_task(self, project, task_id):
        assert project == "skybuild"
        self.reads.append(task_id)
        return dict(self.tasks[task_id])


@pytest.fixture
def catalog(repository, monkeypatch):
    root, source, _, values, member, _ = repository
    monkeypatch.setattr(marshall, "verify_skybuild", lambda checkout: root)
    def build(names, *, conflicting=False):
        for name in names:
            head = member(name, "shared.txt", name + "\n" if conflicting else "same reviewed change\n")
            ref = "refs/heads/task/" + name
            git(root, "branch", "task/" + name, head)
            git(root, "push", "origin", ref)
            values["members"][-1]["ref"] = ref
        path = source.parent / "catalog.json"
        path.write_text(json.dumps({**values, "schema": "skybuild.marshall-input.v1"}))
        return root, path, source.parent / "marshall", API(names)
    return build


def invoke(inputs, **kwargs):
    root, path, output, client = inputs
    return marshall.marshall(root, path, output, client, project="skybuild", principal="owner", **kwargs)


def gate_result(argv, *, ok=True, cleanup=True):
    def value(flag):
        return argv[argv.index(flag) + 1]
    record = {"schema": "skybuild.gate-run.v1", "run_id": value("--run-id"),
              "checkout": value("--checkout"), "head": value("--expected-head"),
              "tree": value("--expected-tree"), "phase": "terminal", "status": "passed" if ok else "failed",
              "cleanup": "confirmed" if cleanup else "unknown", "ok": ok, "exit_code": 0 if ok else 1}
    Path(value("--artifact")).write_text(json.dumps(record))
    return SimpleNamespace(returncode=0 if ok else 1, stdout=json.dumps({"ok": ok, "cleaned_up": cleanup}))


def test_five_related_heads_prepare_without_publication(catalog):
    inputs = catalog(["one", "two", "three", "four", "five"])
    report = invoke(inputs, prepare_next=True)
    assert len(report["bundles"]) == 1
    assert len(report["bundles"][0]["task_ids"]) == 5
    assert report["bundles"][0]["shared_paths"] == ["shared.txt"]
    assert report["prepared"]["ok"] is True
    assert report["published"] is False and report["gate"] is None
    candidate = Path(report["prepared"]["candidate"])
    for member in report["prepared"]["included"]:
        git(candidate, "merge-base", "--is-ancestor", member["head"], "HEAD")
    with pytest.raises(BundlePlanningError, match="new output"):
        invoke(inputs)


@pytest.mark.parametrize('detail,expected', ADMISSION_DETAILS)
def test_admission_diagnostic_is_static_and_preserved_without_candidate(catalog, monkeypatch, detail, expected):
    inputs = catalog(['one'])
    def refuse():
        raise marshall.preparation.PreparationError(detail)
    monkeypatch.setattr(marshall.preparation, '_reserve', refuse)
    with pytest.raises(marshall.preparation.PreparationError):
        invoke(inputs, prepare_next=True)
    report = json.loads((inputs[2] / 'report.json').read_text())
    assert report.get('error_detail') == expected
    assert report['error'] == 'PreparationError'
    assert not report['gate_passed'] and not report['published'] and report['prepared'] is None
    assert not (inputs[2] / 'next/candidate').exists()
    assert 'must-not-appear' not in json.dumps(report) and 'private token' not in json.dumps(report)


@pytest.mark.parametrize('detail,expected', ADMISSION_DETAILS)
def test_cli_admission_diagnostic_suppresses_untrusted_errors(monkeypatch, capsys, tmp_path, detail, expected):
    monkeypatch.setattr(sys, 'argv', ['marshall_bundle.py', '--checkout', str(tmp_path),
        '--catalog', str(tmp_path / 'catalog.json'), '--output', str(tmp_path / 'output'),
        '--token-file', str(tmp_path / 'token'), '--url', 'https://private.invalid',
        '--project', 'skybuild', '--principal', 'owner'])
    monkeypatch.setattr(marshall, '_private_endpoint', lambda *args: None)
    monkeypatch.setattr(marshall, '_token_from_file', lambda *args: 'credential-must-not-appear')
    monkeypatch.setattr(marshall, 'Client', lambda *args, **kwargs: nullcontext(API([])))
    def refuse(*args, **kwargs):
        raise marshall.preparation.PreparationError(detail)
    monkeypatch.setattr(marshall, 'marshall', refuse)
    assert marshall.main() == 1
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report.get('error_detail') == expected
    assert not report['ok'] and report['error'] == 'PreparationError'
    assert all(secret not in output for secret in ('must-not-appear', 'private token'))


def test_blocked_and_in_flight_tasks_do_not_prepare_or_fetch(catalog):
    inputs = catalog(["one", "two"])
    root, path, _, client = inputs
    client.tasks["one"]["status"] = "blocked"
    values = json.loads(path.read_text())
    values["in_flight"] = ["two"]
    path.write_text(json.dumps(values))
    before = git(root, "worktree", "list", "--porcelain")
    report = invoke(inputs, prepare_next=True)
    assert report["bundles"] == [] and report["prepared"] is None
    assert len(report["skipped"]) == 2
    assert client.reads == ["one"]
    assert git(root, "worktree", "list", "--porcelain") == before


def test_task_change_before_preparation_preserves_failure_without_candidate(catalog):
    inputs = catalog(["one"])
    _, _, output, client = inputs
    read = client.get_task
    def change(project, task_id):
        task = read(project, task_id)
        if len(client.reads) > 1:
            task["revision"] = 2
        return task
    client.get_task = change
    with pytest.raises(BundlePlanningError, match="API task changed"):
        invoke(inputs, prepare_next=True)
    report = json.loads((output / "report.json").read_text())
    assert report["error"] == "BundlePlanningError"
    assert not (output / "next" / "candidate").exists()


def test_conflict_is_retained_and_never_reported_green(catalog):
    inputs = catalog(["one", "two"], conflicting=True)
    with pytest.raises(marshall.preparation.PreparationError):
        invoke(inputs, prepare_next=True)
    output = inputs[2]
    child = json.loads((output / "next" / "report.json").read_text())
    assert child["ok"] is False and child["conflicts"] == ["shared.txt"]
    assert (output / "next" / "candidate").exists()
    report = json.loads((output / "report.json").read_text())
    assert report["gate_passed"] is False and report["published"] is False


@pytest.mark.parametrize("ok, cleanup", [(False, True), (True, False), (True, True)])
def test_gate_requires_success_cleanup_and_unchanged_snapshot(catalog, ok, cleanup):
    inputs = catalog(["one"])
    client = inputs[3]
    def gate(argv, **kwargs):
        assert argv[argv.index("--min-available-gib") + 1] == "6"
        assert Path(kwargs["cwd"]).name == "candidate"
        return gate_result(argv, ok=ok, cleanup=cleanup)
    report = invoke(inputs, gate_next=True, gate_runner=gate)
    assert report["gate_passed"] is (ok and cleanup)
    assert report["published"] is False
    if not ok or not cleanup:
        assert report["next_action"].startswith("Resolve failed gate")


def test_task_change_during_passing_gate_blocks_green_handoff(catalog):
    inputs = catalog(["one"])
    def gate(*args, **kwargs):
        inputs[3].tasks["one"]["revision"] = 2
        return gate_result(args[0])
    with pytest.raises(BundlePlanningError, match="API task changed"):
        invoke(inputs, gate_next=True, gate_runner=gate)
    report = json.loads((inputs[2] / "report.json").read_text())
    assert report["gate_passed"] is False


def test_cli_imports_its_own_checkout_with_a_shared_interpreter(tmp_path):
    script = Path(marshall.__file__).resolve()
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment["PYTHONSAFEPATH"] = "1"
    result = subprocess.run([sys.executable, str(script), "--help"], cwd=tmp_path,
                            env=environment, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "--gate-next" in result.stdout


@pytest.mark.parametrize("result", ["[]", "not-json"])
def test_invalid_gate_result_preserves_red_evidence(catalog, result):
    inputs = catalog(["one"])
    def gate(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=result)
    with pytest.raises(ValueError):
        invoke(inputs, gate_next=True, gate_runner=gate)
    report = json.loads((inputs[2] / "report.json").read_text())
    assert report["gate_passed"] is False and report["published"] is False
    assert report["error"] in {"BundlePlanningError", "JSONDecodeError"}


def test_review_requires_complete_contract(catalog):
    inputs = catalog(["one"])
    values = json.loads(inputs[1].read_text())
    del values["members"][0]["review"]["reviewer"]
    inputs[1].write_text(json.dumps(values))
    with pytest.raises(BundlePlanningError):
        invoke(inputs)


def test_changed_frozen_evidence_blocks_green_gate(catalog):
    inputs = catalog(["one"])
    def gate(*args, **kwargs):
        evidence = inputs[2] / "evidence" / "001.txt"
        evidence.chmod(0o600)
        evidence.write_text("changed")
        return gate_result(args[0])
    with pytest.raises(BundlePlanningError, match="Frozen evidence changed"):
        invoke(inputs, gate_next=True, gate_runner=gate)
    report = json.loads((inputs[2] / "report.json").read_text())
    assert report["gate_passed"] is False


def test_gate_cannot_change_head_even_when_tree_is_identical(catalog):
    inputs = catalog(["one"])
    def gate(argv, **kwargs):
        result = gate_result(argv)
        git(Path(kwargs["cwd"]), "-c", "user.name=Fixture", "-c", "user.email=fixture@localhost",
            "commit", "--allow-empty", "-m", "unreviewed empty commit")
        return result
    with pytest.raises(BundlePlanningError, match="head or tree"):
        invoke(inputs, gate_next=True, gate_runner=gate)
    report = json.loads((inputs[2] / "report.json").read_text())
    assert report["gate_passed"] is False


def test_gate_requires_durable_artifact_for_exact_candidate(catalog):
    inputs = catalog(["one"])
    def gate(argv, **kwargs):
        result = gate_result(argv)
        artifact = Path(argv[argv.index("--artifact") + 1])
        record = json.loads(artifact.read_text())
        record["head"] = "f" * 40
        artifact.write_text(json.dumps(record))
        return result
    with pytest.raises(BundlePlanningError, match="artifact"):
        invoke(inputs, gate_next=True, gate_runner=gate)


@pytest.mark.parametrize("valid", [True, False])
def test_petri_selection_requires_current_validation(catalog, valid):
    from test_integration_workflow import task
    inputs = catalog(["one"])
    values = json.loads(inputs[1].read_text())
    snapshot = task()
    snapshot.update(project_id="skybuild", task_id="one")
    token = snapshot["metadata"]["_skybuild_workflow"]["petri"]["token"]
    for value in [token, *token["evidence"]]:
        value.update(project_id="skybuild", task_id="one", source_head=values["members"][0]["head_sha"],
                     target_base=values["base_sha"])
    if not valid:
        token["evidence"] = []
    inputs[3].tasks["one"] = snapshot
    report = invoke(inputs)
    assert bool(report["bundles"]) is valid
    assert bool(report["skipped"]) is not valid
    assert report["published"] is False
    assert report["prepared"] is None
    assert inputs[3].tasks["one"]["revision"] == 1
