import json
import subprocess
import sys
from pathlib import Path

from scripts import session_snapshot


def _checkout(path: Path) -> Path:
    path.mkdir()
    (path / "src/skybuild").mkdir(parents=True)
    (path / "docs/design").mkdir(parents=True)
    (path / "src/skybuild/store.py").write_text("# marker\n", encoding="utf-8")
    (path / "docs/design/architecture.md").write_text("# SkyBuild\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Snapshot Test"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "snapshot@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin",
                    "https://github.com/stonesky-ai/skybuild.git"], check=True)
    subprocess.run(["git", "-C", str(path), "add", "src", "docs"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "fixture"], check=True)
    return path


def test_checkout_snapshot_reports_exact_root_and_bounded_dirty_paths(tmp_path):
    checkout = _checkout(tmp_path / "checkout")
    (checkout / "scratch.txt").write_text("local change\n", encoding="utf-8")

    result = session_snapshot.checkout_snapshot(checkout)

    assert result["status"] == "available"
    assert result["path"] == str(checkout.resolve())
    assert result["git_root"] == str(checkout.resolve())
    assert result["origin"] == {
        "repository": "stonesky-ai/skybuild",
        "fetch_verified": True, "push_verified": True,
    }
    assert len(result["head"]) == 40
    assert result["branch"]
    assert result["dirty"] == {
        "present": True, "path_count": 1, "paths": ["scratch.txt"], "truncated": False,
    }


def test_checkout_snapshot_refuses_wrong_origin(tmp_path):
    checkout = _checkout(tmp_path / "checkout")
    subprocess.run(["git", "-C", str(checkout), "remote", "set-url", "origin",
                    "https://github.com/example/other.git"], check=True)

    result = session_snapshot.checkout_snapshot(checkout)

    assert result == {"status": "unavailable", "reason": "checkout_verification_failed"}


def test_checkout_snapshot_never_echoes_url_credentials(tmp_path):
    checkout = _checkout(tmp_path / "checkout")
    subprocess.run(["git", "-C", str(checkout), "remote", "set-url", "origin",
                    "https://user:SECRET-TOKEN@github.com/stonesky-ai/skybuild.git"], check=True)

    result = session_snapshot.checkout_snapshot(checkout)

    assert result["status"] == "available"
    assert "SECRET-TOKEN" not in json.dumps(result)
    assert result["origin"]["repository"] == "stonesky-ai/skybuild"


def test_task_summary_keeps_only_bounded_fields():
    view = {
        "task": {
            "project_id": "skybuild", "task_id": "TASK-1", "revision": 9,
            "status": "ready", "place": "ready",
            "next_action": "Do bounded work", "blocker": None, "responsible": "owner",
            "evidence_freshness": "unavailable", "description": "SECRET-DESCRIPTION",
            "metadata": {"private": "SECRET-METADATA"},
            "validation": [{"stage": "unit_tests", "state": "passed", "findings": ["SECRET-FINDING"]}],
        },
        "token": {"place": "ready", "attempt_id": "SECRET-ATTEMPT"},
        "available_actions": ["claim"],
        "transitions": [{"secret": "SECRET-TRANSITION"}],
    }

    result = session_snapshot.summarize_task(view, "skybuild", "TASK-1")

    assert result["status"] == "available"
    assert result["task"]["validation"] == [{"stage": "unit_tests", "state": "passed"}]
    assert result["task"]["available_actions"] == ["claim"]
    encoded = json.dumps(result)
    for private_value in ("SECRET-DESCRIPTION", "SECRET-METADATA", "SECRET-FINDING",
                          "SECRET-ATTEMPT", "SECRET-TRANSITION"):
        assert private_value not in encoded


def test_task_fetch_uses_one_workflow_get_only():
    class FakeClient:
        def __init__(self):
            self.calls = []

        def task_workflow(self, project_id, task_id):
            self.calls.append((project_id, task_id))
            return {"task": {"project_id": project_id, "task_id": task_id,
                              "revision": 1, "status": "ready"},
                    "token": {"place": "ready"}, "available_actions": []}

    client = FakeClient()
    result = session_snapshot.fetch_task_snapshot(client, "skybuild", "TASK-1")

    assert result["status"] == "available"
    assert client.calls == [("skybuild", "TASK-1")]


def test_task_fetch_reports_unavailable_without_exception_text():
    class FakeClient:
        def task_workflow(self, project_id, task_id):
            raise RuntimeError("SECRET-ERROR")

    result = session_snapshot.fetch_task_snapshot(FakeClient(), "skybuild", "TASK-1")
    assert result == {"status": "unavailable", "reason": "task_request_failed"}


def test_codegraph_missing_index_skips_cli(tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("CodeGraph must not run without local index")

    monkeypatch.setattr(session_snapshot, "run_bounded", unexpected)
    result = session_snapshot.codegraph_snapshot(tmp_path, "TaskToken")
    assert result == {"status": "unavailable", "reason": "index_missing_or_unsafe"}


def test_codegraph_rejects_wrong_project_before_explore(tmp_path, monkeypatch):
    (tmp_path / ".codegraph").mkdir()
    calls = []
    monkeypatch.setattr(session_snapshot.shutil, "which", lambda name: "/usr/bin/codegraph")

    def fake_run(argv, *, cwd, **kwargs):
        calls.append(argv)
        return "ok", json.dumps({"initialized": True, "projectPath": str(tmp_path / "other")}), 0

    monkeypatch.setattr(session_snapshot, "run_bounded", fake_run)
    result = session_snapshot.codegraph_snapshot(tmp_path, "TaskToken")

    assert result == {"status": "unavailable", "reason": "project_path_mismatch"}
    assert len(calls) == 1


def test_codegraph_explores_only_after_exact_status(tmp_path, monkeypatch):
    (tmp_path / ".codegraph").mkdir()
    calls = []
    monkeypatch.setattr(session_snapshot.shutil, "which", lambda name: "/usr/bin/codegraph")

    def fake_run(argv, *, cwd, **kwargs):
        calls.append(argv)
        if argv[1] == "status":
            return "ok", json.dumps({"initialized": True, "projectPath": str(tmp_path),
                                     "fileCount": 10, "nodeCount": 20}), 0
        return "ok", "bounded source result", 0

    monkeypatch.setattr(session_snapshot, "run_bounded", fake_run)
    result = session_snapshot.codegraph_snapshot(tmp_path, "TaskToken")

    assert result == {"status": "available", "project_path": str(tmp_path),
                      "index": {"file_count": 10, "node_count": 20},
                      "output": "bounded source result", "truncated": False}
    assert calls[0] == ["codegraph", "status", "--json", str(tmp_path)]
    assert calls[1] == ["codegraph", "explore", "--path", str(tmp_path),
                        "--max-files", "3", "TaskToken"]


def test_run_bounded_caps_output_and_stops_timeout():
    state, output, _ = session_snapshot.run_bounded(
        [sys.executable, "-c", "print('x' * 10000)"], cwd=Path.cwd(), output_limit=64,
    )
    assert state == "output_limit"
    assert len(output.encode()) <= 64

    state, output, _ = session_snapshot.run_bounded(
        [sys.executable, "-c", "import time; time.sleep(2)"], cwd=Path.cwd(), timeout=0.05,
    )
    assert state == "timeout"
    assert output == ""
