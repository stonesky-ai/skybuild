"""Safety checks for local workflow orchestration; Docker and CodeGraph are mocked."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


def load(name):
    path = Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py"
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_gate_overrides_live_dsn_and_cleans_up_on_test_failure(tmp_path, monkeypatch):
    module = load("disposable_pg_gate")
    calls = []
    monkeypatch.setenv("SKYBUILD_DSN", "postgresql://LIVE")
    monkeypatch.setenv("SKYBUILD_ROLE_ADMIN_DSN", "postgresql://LIVE")
    monkeypatch.setenv("SKYBUILD_TEST_DSN", "postgresql://LIVE")

    def fake(argv, **kwargs):
        calls.append(argv)
        if argv == ["fake-pytest"]:
            env = kwargs["env"]
            assert "SKYBUILD_DSN" not in env
            assert "SKYBUILD_ROLE_ADMIN_DSN" not in env
            for variable in ("SKYBUILD_TEST_DSN", "SKYBUILD_HTTP_TEST_DSN", "SKYBUILD_IMPORT_TEST_DSN"):
                assert "@127.0.0.1:15432/skybuild_" in env[variable]
                assert "LIVE" not in env[variable]
            kwargs["stdout"].write("2 passed, 1 failed in 0.3s\n")
            return subprocess.CompletedProcess(argv, 1)
        return subprocess.CompletedProcess(argv, 0, "127.0.0.1:15432\n")

    monkeypatch.setattr(module.subprocess, "run", fake)
    result, code = module.run_gate(tmp_path, 10, "postgres:16", ["fake-pytest"])
    assert code == 1 and not result["ok"]
    assert result["cleaned_up"]
    assert calls[-1][:4] == ["docker", "rm", "--force", "--volumes"]
    assert sum("createdb" in call for call in calls) == 3
    assert "1 failed" in result["pytest_summary"]


def test_gate_removes_container_when_start_times_out(tmp_path, monkeypatch):
    module = load("disposable_pg_gate")
    calls = []

    def fake(argv, **kwargs):
        calls.append(argv)
        if "run" in argv:
            raise subprocess.TimeoutExpired(argv, 10)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(module.subprocess, "run", fake)
    result, code = module.run_gate(tmp_path, 10, "postgres:16", ["fake-pytest"])
    assert code == 1 and result["cleaned_up"]
    assert calls[-1][:4] == ["docker", "rm", "--force", "--volumes"]


def test_graph_refuses_other_checkout_before_sync(tmp_path, monkeypatch):
    module = load("codegraph_checkout")
    (tmp_path / ".codegraph").mkdir()
    calls = []

    def fake(argv, **kwargs):
        calls.append(argv)
        stdout = json.dumps({"initialized": True, "projectPath": "/wrong/checkout"}) if "status" in argv else ""
        return subprocess.CompletedProcess(argv, 0, stdout)

    monkeypatch.setattr(module.subprocess, "run", fake)
    with pytest.raises(RuntimeError, match="does not match"):
        module.prepare(tmp_path)
    assert not any("sync" in call for call in calls)


def test_graph_refuses_unignored_index(tmp_path, monkeypatch):
    module = load("codegraph_checkout")

    def fake(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1 if "check-ignore" in argv else 0, "")

    monkeypatch.setattr(module.subprocess, "run", fake)
    with pytest.raises(RuntimeError, match="ignored and untracked"):
        module.prepare(tmp_path)
