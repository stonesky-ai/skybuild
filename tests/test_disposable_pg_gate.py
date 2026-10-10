"""Offline gate lifecycle checks; no Docker, database or service calls."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import disposable_pg_gate as gate

HEAD, TREE = "a" * 40, "b" * 40


@pytest.fixture
def fake_gate(tmp_path, monkeypatch):
    checkout = tmp_path / "candidate"
    checkout.mkdir()
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    path = private / "run.json"
    identity = {"head": HEAD, "tree": TREE}
    monkeypatch.setattr(gate, "candidate_identity", lambda _: dict(identity))
    monkeypatch.setattr(gate.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(gate, "available_memory_bytes", lambda: 16 * 1024**3)
    calls, stages, passwords = [], [], []
    config = {"test_code": 0, "cleanup_code": 0, "test_error": None, "cleanup_error": None}

    def runner(argv, **kwargs):
        calls.append(argv)
        if path.exists() and argv[:2] != ["docker", "rm"]:
            record = json.loads(path.read_text())
            stages.append(record["phase"])
            assert record["run_id"] == "bounded-run"
            assert record["head"] == HEAD and record["tree"] == TREE
            assert path.stat().st_mode & 0o777 == 0o600
        elif path.exists() and "phase" in json.loads(path.read_text()):
            stages.append(json.loads(path.read_text())["phase"])
        assert kwargs["timeout"] <= 60
        if argv[:2] == ["docker", "run"]:
            passwords.append(kwargs["env"]["POSTGRES_PASSWORD"])
            assert kwargs["env"]["POSTGRES_PASSWORD"] not in " ".join(argv)
        if argv[:2] == ["docker", "rm"]:
            assert argv[-1] == calls[0][calls[0].index("--name") + 1]
            if config["cleanup_error"]:
                raise config["cleanup_error"]
            return subprocess.CompletedProcess(argv, config["cleanup_code"])
        if argv == ["fake-test", "private-command-value"]:
            assert kwargs["env"]["UV_PROJECT_ENVIRONMENT"] == str(checkout / '.venv')
            assert kwargs["env"]["PYTHONPATH"] == ':'.join(map(str, [checkout / 'src', checkout / 'scripts', checkout]))
            assert kwargs["env"]["PYTHONSAFEPATH"] == '1'
            assert 'UV_NO_SYNC' not in kwargs['env'] and 'UV_NO_PROJECT' not in kwargs['env']
            assert 'UV_WORKING_DIR' not in kwargs['env']
            assert "SKYBUILD_DSN" not in kwargs["env"]
            assert "SKYBUILD_ROLE_ADMIN_DSN" not in kwargs["env"]
            if config["test_error"]:
                raise config["test_error"]
            kwargs["stdout"].write(passwords[0] + "\n3 passed\n")
            return subprocess.CompletedProcess(argv, config["test_code"])
        return subprocess.CompletedProcess(argv, 0, stdout="127.0.0.1:54321\n")

    monkeypatch.setattr(gate.subprocess, "run", runner)
    monkeypatch.setenv("SKYBUILD_DSN", "private-live-dsn")
    monkeypatch.setenv("SKYBUILD_ROLE_ADMIN_DSN", "private-admin-dsn")
    monkeypatch.setenv('UV_PROJECT_ENVIRONMENT', '/wrong/checkout/.venv')
    monkeypatch.setenv('PYTHONPATH', '/wrong/checkout/src')
    monkeypatch.setenv('UV_NO_SYNC', '1')
    monkeypatch.setenv('UV_NO_PROJECT', '1')
    monkeypatch.setenv('UV_WORKING_DIR', '/wrong/checkout')

    def run(**changes):
        options = dict(artifact_path=path, run_id="bounded-run", expected_head=HEAD, expected_tree=TREE)
        options.update(changes)
        return gate.run_gate(checkout, 60, "private-image-value", ["fake-test", "private-command-value"],
                             min_available_bytes=10 * 1024**3, **options)

    return run, path, calls, stages, config, identity, passwords


def test_success_is_durable_bound_and_secret_free(fake_gate):
    run, path, calls, stages, _, _, passwords = fake_gate
    result, code = run()
    record = json.loads(path.read_text())
    assert code == 0 and result["ok"] and result["cleaned_up"]
    assert record["status"] == record["gate_outcome"] == "passed"
    assert record["phase"] == "terminal" and record["cleanup"] == "confirmed"
    assert record["exit_code"] == 0 and record["monotonic_deadline"] == 160
    assert record["sequence"] == 6
    assert stages == ["starting_postgres", "waiting_postgres", "waiting_postgres",
                      "creating_databases", "creating_databases", "creating_databases", "testing", "cleaning_up"]
    assert record["container"] == calls[0][calls[0].index("--name") + 1]
    assert record["log"] == result["log"] and record["invocation_id"]
    for secret in passwords + ["private-command-value", "private-image-value", "private-live-dsn", "private-admin-dsn"]:
        assert secret not in path.read_text()
    assert passwords[0] not in Path(result["log"]).read_text()
    assert "pytest_summary" not in record


def test_shared_gate_environment_imports_selected_checkout_in_real_child(tmp_path, monkeypatch):
    checkout = Path(__file__).resolve().parents[1]
    foreign = tmp_path / 'foreign checkout'
    (foreign / 'scripts').mkdir(parents=True)
    (foreign / 'scripts/__init__.py').write_text('raise AssertionError("foreign scripts imported")\n')
    for name in ('skybuild', '_project_environment'):
        (foreign / (name + '.py')).write_text('raise AssertionError("foreign module imported")\n')
    monkeypatch.setenv('PYTHONPATH', str(foreign))
    monkeypatch.setenv('UV_WORKING_DIR', str(foreign))
    monkeypatch.setenv('UV_PROJECT_ENVIRONMENT', str(foreign / '.venv'))
    env = gate.project_environment(checkout)
    # The default gate uses this environment with Python directly, bypassing project_python.
    result = subprocess.run([sys.executable, '-c', '''import json, os
import scripts.manual_pilot_listener, _project_environment, skybuild
print(json.dumps({
    "listener": scripts.manual_pilot_listener.__file__,
    "helper": _project_environment.__file__,
    "package": skybuild.__file__,
    "cwd": os.getcwd(), "working_dir": os.environ.get("UV_WORKING_DIR"),
}))
'''], cwd=foreign, env=env, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        'listener': str(checkout / 'scripts/manual_pilot_listener.py'),
        'helper': str(checkout / 'scripts/_project_environment.py'),
        'package': str(checkout / 'src/skybuild/__init__.py'),
        'cwd': str(foreign), 'working_dir': None,
    }


@pytest.mark.parametrize("error,status", [(KeyboardInterrupt(), "interrupted"),
    (subprocess.TimeoutExpired("fake", 60), "timed_out"), (RuntimeError("private error"), "error")])
def test_interruption_and_timeout_keep_cleanup_evidence(fake_gate, error, status):
    run, path, _, _, config, _, _ = fake_gate
    config["test_error"] = error
    result, code = run()
    record = json.loads(path.read_text())
    assert code == 1 and not result["ok"] and result["cleaned_up"]
    assert record["status"] == status and record["cleanup"] == "confirmed"
    assert "private error" not in path.read_text()


def test_test_failure_remains_distinct(fake_gate):
    run, path, _, _, config, _, _ = fake_gate
    config["test_code"] = 7
    result, code = run()
    assert code == 7 and not result["ok"]
    assert json.loads(path.read_text())["status"] == "failed"


@pytest.mark.parametrize("error", [None, OSError("private cleanup error")])
def test_unconfirmed_cleanup_never_reports_success(fake_gate, error):
    run, path, _, _, config, _, _ = fake_gate
    config.update(cleanup_code=1, cleanup_error=error)
    result, code = run()
    record = json.loads(path.read_text())
    assert code == 1 and not result["ok"] and not result["cleaned_up"]
    assert record["status"] == "cleanup_unconfirmed" and record["cleanup"] == "unknown"
    assert record["gate_outcome"] == "passed"  # Test result is separate from lifecycle success.


@pytest.mark.parametrize("changes", [{"expected_head": "c" * 40}, {"expected_tree": "c" * 40},
    {"run_id": "bad/run"}, {"run_id": None}])
def test_wrong_pins_or_run_refuse_before_docker(fake_gate, changes):
    run, path, calls, *_ = fake_gate
    result, code = run(**changes)
    assert code == 1 and result["error"] == "ArtifactError" and not calls and not path.exists()


@pytest.mark.parametrize("replacement_run", ["bounded-run", "other-run"])
def test_existing_output_is_never_reused(fake_gate, replacement_run):
    run, path, calls, *_ = fake_gate
    run()
    original = path.read_bytes()
    calls.clear()
    result, code = run(run_id=replacement_run)
    assert code == 1 and result["error"] == "ArtifactError" and not calls
    assert path.read_bytes() == original


def test_public_artifact_directory_refuses_before_effect(fake_gate):
    run, path, calls, *_ = fake_gate
    path.parent.chmod(0o755)
    result, code = run()
    assert code == 1 and result["error"] == "ArtifactError" and not calls and not path.exists()


def test_symlink_output_preserves_target_and_refuses(fake_gate):
    run, path, calls, *_ = fake_gate
    target = path.parent / "original.json"
    target.write_bytes(b"preserved evidence")
    path.symlink_to(target)
    result, code = run()
    assert code == 1 and not calls and target.read_bytes() == b"preserved evidence"


@pytest.mark.parametrize("phase", ["waiting_postgres", "cleaning_up", "terminal"])
def test_lost_update_is_visible_and_owned_cleanup_still_runs(fake_gate, monkeypatch, phase):
    run, path, calls, *_ = fake_gate
    update = gate.RunArtifact.update
    def fail(self, current, **values):
        if current == phase:
            raise OSError("private write failure")
        return update(self, current, **values)
    monkeypatch.setattr(gate.RunArtifact, "update", fail)
    result, code = run()
    assert code == 1 and not result["ok"] and result["cleaned_up"]
    assert calls[-1][:2] == ["docker", "rm"]
    record = json.loads(path.read_text())
    if phase == "waiting_postgres":
        assert record["status"] == "error"
    else:
        assert result["artifact_error"] and record["status"] == "running"


def test_initial_fsync_failure_reserves_incomplete_evidence(fake_gate, monkeypatch):
    run, path, calls, *_ = fake_gate
    monkeypatch.setattr(gate.os, "fsync", lambda _: (_ for _ in ()).throw(OSError("disk failure")))
    result, code = run()
    assert code == 1 and result["error"] == "ArtifactError" and not calls
    assert path.exists() and json.loads(path.read_text())["status"] == "running"


@pytest.mark.parametrize("repeated_fsync_failure", [False, True])
def test_terminal_directory_fsync_failure_invalidates_visible_success(fake_gate, monkeypatch, repeated_fsync_failure):
    run, path, calls, *_ = fake_gate
    sync_directory = gate.RunArtifact.sync_directory
    reached = []
    def fail_terminal(self):
        if self.record["phase"] == "terminal":
            # The real update has already replaced the artifact with success.
            assert json.loads(path.read_text())["ok"] is True
            reached.append(True)
            if repeated_fsync_failure:
                monkeypatch.setattr(gate.os, "fsync", lambda _: (_ for _ in ()).throw(OSError("disk failure")))
            raise OSError("directory fsync failure")
        return sync_directory(self)
    monkeypatch.setattr(gate.RunArtifact, "sync_directory", fail_terminal)
    result, code = run()
    record = json.loads(path.read_text())
    assert reached == [True] and calls[-1][:2] == ["docker", "rm"]
    assert code == 1 and not result["ok"] and result["artifact_error"] and result["cleaned_up"]
    assert record["status"] == "reporting_unconfirmed" and record["ok"] is False
    assert record["exit_code"] == 1 and record["durability"] == "unknown"
    assert record["gate_outcome"] == "passed" and record["cleanup"] == "confirmed"
    assert record["head"] == HEAD and record["tree"] == TREE and record["run_id"] == "bounded-run"
    original = path.read_bytes()
    assert run()[1] == 1 and path.read_bytes() == original  # Output stays reserved.


def test_terminal_invalidation_preserves_conflicting_evidence(fake_gate, monkeypatch):
    run, path, *_ = fake_gate
    sync_directory = gate.RunArtifact.sync_directory
    foreign = b'{"run_id":"foreign"}\n'
    def fail_terminal(self):
        if self.record["phase"] == "terminal":
            path.write_bytes(foreign)
            raise OSError("directory fsync failure")
        return sync_directory(self)
    monkeypatch.setattr(gate.RunArtifact, "sync_directory", fail_terminal)
    result, code = run()
    assert code == 1 and not result["ok"] and result["artifact_error"]
    assert path.read_bytes() == foreign


def test_unwritable_terminal_invalidation_retains_explicit_cli_failure(fake_gate, monkeypatch):
    run, path, *_ = fake_gate
    sync_directory = gate.RunArtifact.sync_directory
    open_file = gate.os.open
    def refuse_writes(path, flags, *args, **kwargs):
        if flags & gate.os.O_RDWR:
            raise OSError("filesystem became read-only")
        return open_file(path, flags, *args, **kwargs)
    def fail_terminal(self):
        if self.record["phase"] == "terminal":
            monkeypatch.setattr(gate.os, "open", refuse_writes)
            raise OSError("directory fsync failure")
        return sync_directory(self)
    monkeypatch.setattr(gate.RunArtifact, "sync_directory", fail_terminal)
    result, code = run()
    assert code == 1 and not result["ok"] and result["artifact_error"]
    # No program can guarantee a correction when the filesystem refuses writes.
    # Terminal-looking bytes alone must not override this failed invocation.
    assert json.loads(path.read_text())["ok"] is True


def test_changed_record_is_not_overwritten(fake_gate, monkeypatch):
    run, path, calls, *_ = fake_gate
    update = gate.RunArtifact.update
    foreign = b'{"run_id":"foreign"}\n'
    def replace(self, phase, **values):
        if phase == "waiting_postgres":
            path.write_bytes(foreign)
        return update(self, phase, **values)
    monkeypatch.setattr(gate.RunArtifact, "update", replace)
    result, code = run()
    assert code == 1 and result["artifact_error"] and result["cleaned_up"]
    assert path.read_bytes() == foreign and calls[-1][:2] == ["docker", "rm"]


def test_candidate_drift_prevents_tests_and_acceptance(fake_gate, monkeypatch):
    run, path, calls, _, _, identity, _ = fake_gate
    update = gate.RunArtifact.update
    def drift(self, phase, **values):
        update(self, phase, **values)
        if phase == "creating_databases":
            identity["tree"] = "c" * 40
    monkeypatch.setattr(gate.RunArtifact, "update", drift)
    result, code = run()
    assert code == 1 and result["artifact_error"] and result["cleaned_up"]
    assert not any(argv[0] == "fake-test" for argv in calls)
    record = json.loads(path.read_text())
    assert record["status"] == "candidate_changed_or_unreadable" and record["cleanup"] == "confirmed"


def test_terminal_candidate_drift_cannot_accept_passed_tests(fake_gate, monkeypatch):
    run, path, _, _, _, identity, _ = fake_gate
    update = gate.RunArtifact.update
    def drift(self, phase, **values):
        update(self, phase, **values)
        if phase == "cleaning_up":
            identity["head"] = "c" * 40
    monkeypatch.setattr(gate.RunArtifact, "update", drift)
    result, code = run()
    assert code == 1 and not result["ok"] and result["cleaned_up"]
    record = json.loads(path.read_text())
    assert record["status"] == "candidate_changed_or_unreadable" and record["ok"] is False
    assert record["head"] == HEAD and record["tree"] == TREE


def test_no_headroom_has_confirmed_no_start(fake_gate, monkeypatch):
    run, path, calls, *_ = fake_gate
    monkeypatch.setattr(gate, "available_memory_bytes", lambda: 1)
    result, code = run()
    assert code == 1 and not calls and "cleaned_up" not in result
    expected = "Available memory below gate minimum: 1 bytes available; 10737418240 bytes required"
    assert result["error_detail"] == expected
    record = json.loads(path.read_text())
    assert record["cleanup"] == "not_started"
    assert record["error_detail"] == expected


def test_default_output_remains_unchanged(fake_gate):
    run, _, _, _, _, _, _ = fake_gate
    result, code = run(artifact_path=None, run_id=None, expected_head=None, expected_tree=None)
    assert code == 0 and set(result) == {"ok", "log", "container", "cleaned_up", "pytest_summary", "exit_code"}


def test_candidate_identity_uses_actual_clean_git_root(tmp_path):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], stderr=subprocess.DEVNULL).decode().strip()
    git("init"); git("config", "user.name", "Test"); git("config", "user.email", "test@localhost")
    (tmp_path / "source").write_text("pinned")
    git("add", "source"); git("commit", "-m", "candidate")
    assert gate.candidate_identity(tmp_path) == {"head": git("rev-parse", "HEAD"), "tree": git("rev-parse", "HEAD^{tree}")}
    (tmp_path / "source").write_text("drift")
    with pytest.raises(gate.ArtifactError):
        gate.candidate_identity(tmp_path)
