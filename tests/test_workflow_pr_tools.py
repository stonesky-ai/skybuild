"""Offline workflow tests: no GitHub writes or live database access."""
import importlib.util
import json
from contextlib import nullcontext
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_pr_body_preserves_literal_utf8_and_newlines(tmp_path, monkeypatch, capsys):
    module = load("pr_text")
    body = tmp_path / "body.md"
    text = "First line\n\nLiteral `$(echo unsafe)` and café\n"
    body.write_bytes(text.encode())
    calls = []
    monkeypatch.setattr(module, "verify_skybuild", lambda path: path)
    def fake_run(argv, cwd=None, input_text=None):
        calls.append((argv, input_text))
        return json.dumps({"body": text})
    monkeypatch.setattr(module, "run", fake_run)
    module.main(["body", "--pr", "12", "--file", str(body), "--checkout", str(tmp_path)])
    assert calls[0][0] == ["gh", "api", "--method", "PATCH", "repos/stonesky-ai/skybuild/pulls/12", "--input", "-"]
    assert json.loads(calls[0][1]) == {"body": text}
    assert calls[1][0] == ["gh", "api", "repos/stonesky-ai/skybuild/pulls/12"]
    assert len(calls) == 2
    assert json.loads(capsys.readouterr().out)["verified"]


def test_pr_body_mismatch_fails(tmp_path, monkeypatch):
    module = load("pr_text")
    body = tmp_path / "body.md"
    body.write_text("expected\n")
    monkeypatch.setattr(module, "verify_skybuild", lambda path: path)
    monkeypatch.setattr(module, "run", lambda argv, cwd=None, input_text=None: '{"body":"wrong"}')
    with pytest.raises(RuntimeError, match="does not match"):
        module.main(["body", "--pr", "12", "--file", str(body), "--checkout", str(tmp_path)])


def test_pr_comment_uses_rest_and_verifies_response(tmp_path, monkeypatch, capsys):
    module = load("pr_text")
    body = tmp_path / "comment.md"
    body.write_text("Summary\n\n- feature\n")
    calls = []
    monkeypatch.setattr(module, "verify_skybuild", lambda path: path)

    def fake_run(argv, cwd=None, input_text=None):
        calls.append((argv, input_text))
        return input_text

    monkeypatch.setattr(module, "run", fake_run)
    module.main(["comment", "--pr", "12", "--file", str(body), "--checkout", str(tmp_path)])
    assert calls[0][0] == ["gh", "api", "--method", "POST", "repos/stonesky-ai/skybuild/issues/12/comments", "--input", "-"]
    assert json.loads(calls[0][1]) == {"body": body.read_text()}
    assert json.loads(capsys.readouterr().out)["verified"]


@pytest.mark.parametrize("change", ["gate_failure", "base_changed", "wrong_initial_base", "pass", "default_gate"])
def test_candidate_gate_cleanup_and_ref_checks(tmp_path, monkeypatch, change):
    module = load("integrate_reviewed_pr")
    head, base = "a" * 40, "b" * 40
    evidence = tmp_path / "review.txt"
    evidence.write_text("Independent reviewer approved " + head + " against " + base)
    args = SimpleNamespace(checkout=tmp_path, review_evidence=evidence, remote="origin",
                           repo=None, expected_head=head, expected_base=base, base="dev-002", pr=12,
                           gate_argv=None if change == "default_gate" else ["fake-gate", "{checkout}"], merge=False)
    monkeypatch.setattr(module, "verify_skybuild", lambda path: path)
    monkeypatch.setattr(module, "verify_skybuild_remote", lambda *_: None)
    monkeypatch.setattr(module, "reserve_worktree_slots", lambda *_: nullcontext())
    calls = []
    base_reads = 0
    def fake_run(argv, cwd):
        nonlocal base_reads
        calls.append(argv)
        if argv[:3] == ["git", "remote", "get-url"]:
            return "git@github.com:stonesky-ai/skybuild.git"
        if argv[:3] == ["gh", "pr", "view"]:
            return json.dumps(dict(state="OPEN", isDraft=False, headRefOid=head,
                                   baseRefName="dev-002", mergeable="MERGEABLE", mergeStateStatus="CLEAN"))
        if "ls-remote" in argv:
            if argv[-1] == "refs/heads/dev-002":
                base_reads += 1
                oid = "c" * 40 if change == "wrong_initial_base" or change == "base_changed" and base_reads > 1 else base
            else:
                oid = head
            return oid + "\t" + argv[-1]
        if "rev-parse" in argv:
            return "d" * 40
        return ""
    gate_commands = []
    def fake_gate(argv, cwd):
        gate_commands.append(argv)
        if change == "gate_failure":
            raise RuntimeError("gate failed")
        return {"ok": True, "passed": 17}
    monkeypatch.setattr(module, "run", fake_run)
    monkeypatch.setattr(module, "run_gate", fake_gate)
    if change in {"pass", "default_gate"}:
        result = module.integrate(args)
        assert result["merged"] is False
        assert result["gate"] == {"ok": True, "passed": 17}
        assert result["expected_base"] == base
        if change == "default_gate":
            assert gate_commands[0][-2:] == ["--min-available-gib", "10"]
    else:
        with pytest.raises(RuntimeError, match="gate failed|Remote refs changed|Remote base differs"):
            module.integrate(args)
    if change != "wrong_initial_base":
        assert any(argv[:3] == ["git", "worktree", "remove"] for argv in calls)
    else:
        assert not any(argv[:3] == ["git", "worktree", "add"] for argv in calls)
    assert not any(argv[:3] == ["gh", "pr", "merge"] for argv in calls)


def test_failed_gate_retains_log_without_test_output(monkeypatch, tmp_path):
    module = load("integrate_reviewed_pr")
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=1, stdout='{"ok":false,"error_detail":"Available memory below gate minimum: '
                            '1 bytes available; 10737418240 bytes required",'
                            '"log":"/tmp/skybuild-gate.log"}', stderr="secret stderr"))
    with pytest.raises(RuntimeError) as failure:
        module.run_gate(["gate"], tmp_path)
    assert "reason=Available memory below gate minimum: 1 bytes available; 10737418240 bytes required" in str(failure.value)
    assert "log=/tmp/skybuild-gate.log" in str(failure.value)
    assert "secret" not in str(failure.value)


def test_failed_gate_hides_untrusted_error_detail(monkeypatch, tmp_path):
    module = load("integrate_reviewed_pr")
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=1, stdout='{"ok":false,"error_detail":"private secret detail"}', stderr=""))
    with pytest.raises(RuntimeError) as failure:
        module.run_gate(["gate"], tmp_path)
    assert "private secret detail" not in str(failure.value)


def test_custom_gate_cannot_publish(monkeypatch, tmp_path):
    module = load("integrate_reviewed_pr")
    monkeypatch.setattr(module, "verify_skybuild", lambda path: path)
    args = SimpleNamespace(checkout=tmp_path, merge=True, gate_argv=["true"])
    with pytest.raises(RuntimeError, match="Custom gates are validation-only"):
        module.integrate(args)
