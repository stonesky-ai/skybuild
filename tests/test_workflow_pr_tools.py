"""Offline workflow tests: no GitHub writes or live database access."""
import importlib.util
import json
from contextlib import nullcontext
from pathlib import Path
import sys
import threading
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


@pytest.mark.parametrize("change", [
    "gate_failure", "base_changed", "base_changed_waiting", "wrong_initial_base", "pass", "overlap", "default_gate", "default_gate_publish",
    "head_changed", "custom_head_changed", "wrong_artifact", "nonterminal_artifact",
    "missing_artifact", "cleanup_unknown", "malformed_artifact", "wrong_run", "gate_malformed",
])
def test_candidate_gate_cleanup_and_ref_checks(tmp_path, monkeypatch, change):
    module = load("integrate_reviewed_pr")
    head, base = "a" * 40, "b" * 40
    evidence = tmp_path / "review.txt"
    evidence.write_text("Independent reviewer approved " + head + " against " + base)
    default_gate = change in {"default_gate", "default_gate_publish", "head_changed", "wrong_artifact", "nonterminal_artifact",
                              "missing_artifact", "cleanup_unknown", "malformed_artifact", "wrong_run", "gate_malformed"}
    args = SimpleNamespace(checkout=tmp_path, review_evidence=evidence, remote="origin",
                           repo=None, expected_head=head, expected_base=base, base="dev-002", pr=12,
                           gate_argv=None if default_gate else ["fake-gate", "{checkout}"],
                           merge=default_gate and change != "default_gate")
    monkeypatch.setattr(module, "verify_skybuild", lambda path: path)
    monkeypatch.setattr(module, "verify_skybuild_remote", lambda *_: None)
    monkeypatch.setattr(module, "reserve_worktree_slots", lambda *_: nullcontext())
    monkeypatch.setattr(module, "serialize_integrations", lambda *_: nullcontext(), raising=False)
    if change == "overlap":
        import _worktree_capacity as capacity
        monkeypatch.setattr(capacity, "_git", lambda *_: str(tmp_path))
        monkeypatch.setattr(capacity, "_worktree_count", lambda _: 1)
        monkeypatch.setattr(module, "reserve_worktree_slots", capacity.reserve_worktree_slots)
        monkeypatch.setattr(module, "serialize_integrations", capacity.serialize_integrations, raising=False)
    calls = []
    base_reads = 0
    def fake_run(argv, cwd):
        nonlocal base_reads
        calls.append(argv)
        if argv[:3] == ["git", "remote", "get-url"]:
            return "git@github.com:stonesky-ai/skybuild.git"
        if argv[:3] == ["gh", "pr", "view"]:
            if "mergeCommit" in argv[-1]:
                return json.dumps({"state": "MERGED", "mergeCommit": {"oid": "f" * 40}})
            return json.dumps(dict(state="OPEN", isDraft=False, headRefOid=head,
                                   baseRefName="dev-002", mergeable="MERGEABLE", mergeStateStatus="CLEAN"))
        if "ls-remote" in argv:
            if argv[-1] == "refs/heads/dev-002":
                base_reads += 1
                changed = (change == "wrong_initial_base"
                           or change == "base_changed" and base_reads > 2
                           or change == "base_changed_waiting" and base_reads > 1)
                oid = "c" * 40 if changed else base
            else:
                oid = head
            return oid + "\t" + argv[-1]
        if "rev-parse" in argv:
            if argv[-1] == "HEAD" and gate_commands and change in {"head_changed", "custom_head_changed"}:
                return "e" * 40
            return "d" * 40
        return ""
    gate_commands = []
    def fake_gate(argv, cwd):
        gate_commands.append(argv)
        if change == "gate_failure":
            raise RuntimeError("gate failed")
        if change == "overlap":
            entered = threading.Event()
            def prepare_during_gate():
                with capacity.reserve_worktree_slots(tmp_path, 2):
                    entered.set()
            thread = threading.Thread(target=prepare_during_gate, daemon=True)
            thread.start()
            assert entered.wait(1), "Integration retained the capacity lock during its gate"
            thread.join(1)
            assert not thread.is_alive()
        if "--artifact" in argv:
            def value(flag):
                return argv[argv.index(flag) + 1]
            record = {
                "schema": "skybuild.gate-run.v1", "run_id": value("--run-id"),
                "checkout": str(cwd), "head": value("--expected-head"), "tree": value("--expected-tree"),
                "phase": "terminal", "status": "passed", "cleanup": "confirmed", "ok": True, "exit_code": 0}
            if change == "wrong_artifact":
                record["head"] = "f" * 40
            if change == "nonterminal_artifact":
                record["phase"] = "running"
            if change == "wrong_run":
                record["run_id"] = "another-run"
            if change != "missing_artifact":
                Path(value("--artifact")).write_text("not-json" if change == "malformed_artifact" else json.dumps(record))
        if change == "gate_malformed":
            return []
        return {"ok": True, "passed": 17, "cleaned_up": change != "cleanup_unknown"}
    monkeypatch.setattr(module, "run", fake_run)
    monkeypatch.setattr(module, "run_gate", fake_gate)
    if change in {"pass", "overlap", "default_gate", "default_gate_publish"}:
        result = module.integrate(args)
        assert result["merged"] is (change == "default_gate_publish")
        assert result["gate"] == {"ok": True, "passed": 17, "cleaned_up": True}
        assert result["expected_base"] == base
        if default_gate:
            assert gate_commands[0][gate_commands[0].index("--min-available-gib") + 1] == "6"
            assert gate_commands[0][gate_commands[0].index("--expected-head") + 1] == "d" * 40
            assert Path(result["gate_artifact"]).is_file()
    else:
        with pytest.raises((OSError, RuntimeError, ValueError)) as failure:
            module.integrate(args)
        if default_gate:
            artifact = gate_commands[0][gate_commands[0].index("--artifact") + 1]
            assert "artifact=" + artifact in str(failure.value)
    if change not in {"wrong_initial_base", "base_changed_waiting"}:
        assert any(argv[:3] == ["git", "worktree", "remove"] for argv in calls)
    else:
        assert not any(argv[:3] == ["git", "worktree", "add"] for argv in calls)
        assert gate_commands == []
    assert any(argv[:3] == ["gh", "pr", "merge"] for argv in calls) is (change == "default_gate_publish")


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
