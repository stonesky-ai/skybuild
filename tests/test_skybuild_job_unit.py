"""The unit adapter never launches a real process in these tests."""

import json
import subprocess
from pathlib import Path

import pytest

from scripts.skybuild_job_unit import JobSpec, JobUnitError, JobUnitManager


class FakeSystemd:
    def __init__(self):
        self.calls = []
        self.show = "LoadState=not-found\nActiveState=inactive\n"
        self.run_status = 0
        self.invocation = "a" * 32
        self.description = ""

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if argv[0] == "systemctl" and argv[1:3] == ["--user", "show"]:
            identity = f"InvocationID={self.invocation}\nDescription={self.description}\n" if "LoadState=loaded\n" in self.show else ""
            return subprocess.CompletedProcess(argv, 0, self.show + identity, "")
        if argv[0] == "systemd-run":
            self.description = next(arg.removeprefix("Description=") for arg in argv if arg.startswith("Description="))
            self.show = "LoadState=loaded\nActiveState=active\nSubState=running\n"
            return subprocess.CompletedProcess(argv, self.run_status, "", "failed" if self.run_status else "")
        return subprocess.CompletedProcess(argv, 0, "", "")


def spec(tmp_path: Path, **changes) -> JobSpec:
    worktree = tmp_path / "worktree"
    worktree.mkdir(exist_ok=True)
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Do one admitted task.\n")
    values = dict(task_id="TASK-1", attempt_id="attempt-1", worktree=worktree,
                  argv=("/usr/bin/true",), stdin_path=prompt, log_path=tmp_path / "job.log",
                  memory_high_bytes=1024**3, memory_max_bytes=2 * 1024**3,
                  runtime_seconds=3600, environment={"HOME": str(tmp_path), "PATH": "/usr/bin"})
    values.update(changes)
    return JobSpec(**values)


def test_launch_is_niced_capped_isolated_and_durable(tmp_path):
    fake = FakeSystemd()
    manager = JobUnitManager(tmp_path / "state", run=fake)
    job = spec(tmp_path)
    unit = manager.start(job)
    assert unit == job.unit()
    manifest_path = tmp_path / "state" / f"{unit}.json"
    manifest = json.loads(manifest_path.read_text())
    assert manifest["phase"] == "launch_intent"
    assert manifest["memory_max_bytes"] == 2 * 1024**3
    assert manifest_path.stat().st_mode & 0o777 == 0o600
    command = next(argv for argv, _ in fake.calls if argv[0] == "systemd-run")
    assert manifest["invocation_id"] == fake.invocation
    assert f"Description=SkyBuild launch {manifest['launch_nonce']}" in command
    props = [command[index + 1] for index, item in enumerate(command) if item == "-p"]
    assert {"Nice=10", "MemoryHigh=1073741824", "MemoryMax=2147483648", "MemorySwapMax=0",
            "RuntimeMaxSec=3600", "NoNewPrivileges=yes", "RemainAfterExit=no",
            "KillMode=control-group", "TimeoutStopSec=10", "SendSIGKILL=yes", "OOMPolicy=kill",
            f"WorkingDirectory={job.worktree}"} <= set(props)
    assert command[command.index("--") + 1:command.index("--") + 3] == ["/usr/bin/env", "-i"]
    assert command[-1] == "/usr/bin/true"
    assert "--expand-environment=no" in command
    assert not any("GH_TOKEN" in item for item in command)


def test_existing_intent_prevents_replay_after_restart(tmp_path):
    fake = FakeSystemd()
    job = spec(tmp_path)
    JobUnitManager(tmp_path / "state", run=fake).start(job)
    fake.calls.clear()
    with pytest.raises(JobUnitError, match="launch intent already exists"):
        JobUnitManager(tmp_path / "state", run=fake).start(job)
    assert not any(call[0][0] == "systemd-run" for call in fake.calls)


def test_attempt_cannot_replay_from_another_worktree(tmp_path):
    fake = FakeSystemd()
    manager = JobUnitManager(tmp_path / "state", run=fake)
    job = spec(tmp_path)
    manager.start(job)
    other = tmp_path / "other"
    other.mkdir()
    assert spec(tmp_path, worktree=other).unit() == job.unit()
    with pytest.raises(JobUnitError, match="launch intent already exists"):
        manager.start(spec(tmp_path, worktree=other))
    assert len([call for call, _ in fake.calls if call[0] == "systemd-run"]) == 1


def test_uncertain_systemd_failure_keeps_intent(tmp_path):
    fake = FakeSystemd()
    fake.run_status = 1
    job = spec(tmp_path)
    manager = JobUnitManager(tmp_path / "state", run=fake)
    with pytest.raises(JobUnitError, match="launch intent retained"):
        manager.start(job)
    assert json.loads((tmp_path / "state" / f"{job.unit()}.json").read_text())["phase"] == "launch_intent"
    # A later successful show must not adopt an invocation after a lost reply.
    with pytest.raises(JobUnitError, match="unpinned"):
        manager.observe(job.unit())


def test_observe_running_and_completed_retains_peak(tmp_path):
    fake = FakeSystemd()
    manager = JobUnitManager(tmp_path / "state", run=fake)
    unit = manager.start(spec(tmp_path))
    fake.show = ("LoadState=loaded\nActiveState=active\nSubState=running\n"
                 "ControlGroup=/user.slice/job.service\nMemoryCurrent=1024\nMemoryPeak=2048\n")
    running = manager.observe(unit)
    assert running.phase == "running" and running.memory_peak_bytes == 2048
    fake.show = ("LoadState=loaded\nActiveState=active\nSubState=exited\nResult=success\n"
                 "ExecMainStatus=0\nMemoryCurrent=18446744073709551615\n")
    ended = manager.observe(unit)
    assert ended.phase == "completed" and ended.exit_status == 0
    manifest = json.loads((tmp_path / "state" / f"{unit}.json").read_text())
    assert manifest["memory_peak_bytes"] == 2048
    assert manifest["control_group"] == "/user.slice/job.service"
    fake.show = "LoadState=not-found\nActiveState=inactive\n"
    assert manager.observe(unit).phase == "completed"


def test_no_user_bus_fails_before_launch_intent(tmp_path):
    fake = FakeSystemd()

    def fail_show(argv, **kwargs):
        if argv[0] == "systemctl":
            return subprocess.CompletedProcess(argv, 1, "", "Failed to connect to user scope bus")
        return fake(argv, **kwargs)

    job = spec(tmp_path)
    with pytest.raises(JobUnitError, match="systemctl show failed"):
        JobUnitManager(tmp_path / "state", run=fail_show).start(job)
    assert not (tmp_path / "state" / f"{job.unit()}.json").exists()


def test_unrecognized_load_state_fails_closed(tmp_path):
    fake = FakeSystemd()
    fake.show = "LoadState=masked\nActiveState=inactive\n"
    job = spec(tmp_path)
    with pytest.raises(JobUnitError, match="unknown load state"):
        JobUnitManager(tmp_path / "state", run=fake).start(job)
    assert not (tmp_path / "state" / f"{job.unit()}.json").exists()


def test_unknown_unit_is_not_stopped(tmp_path):
    fake = FakeSystemd()
    (tmp_path / "state").mkdir(mode=0o700)
    manager = JobUnitManager(tmp_path / "state", run=fake)
    with pytest.raises(JobUnitError, match="manifest unavailable"):
        manager.stop("skybuild-job-" + "a" * 24 + ".service")
    assert fake.calls == []


def test_world_readable_state_dir_refuses_launch(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    state_dir.chmod(0o755)
    fake = FakeSystemd()
    with pytest.raises(JobUnitError, match="mode 0700"):
        JobUnitManager(state_dir, run=fake).start(spec(tmp_path))
    assert fake.calls == []


@pytest.mark.parametrize("changes", [
    {"memory_high_bytes": 2 * 1024**3},
    {"runtime_seconds": 8 * 3600 + 1},
    {"environment": {"GH_TOKEN": "secret"}},
    {"argv": ("true",)},
    {"task_id": "bad/name"},
])
def test_invalid_spec_starts_nothing(tmp_path, changes):
    fake = FakeSystemd()
    with pytest.raises(JobUnitError):
        JobUnitManager(tmp_path / "state", run=fake).start(spec(tmp_path, **changes))
    assert fake.calls == []


@pytest.mark.parametrize("replacement", ["b" * 32, "", "0" * 32])
def test_reused_or_missing_invocation_never_overwrites_owned_evidence(tmp_path, replacement):
    fake = FakeSystemd()
    manager = JobUnitManager(tmp_path / "state", run=fake)
    unit = manager.start(spec(tmp_path))
    manager.observe(unit)
    path = tmp_path / "state" / f"{unit}.json"
    before = path.read_bytes()
    fake.invocation = replacement
    fake.show += "MemoryPeak=999999\n"
    with pytest.raises(JobUnitError, match="differs"):
        JobUnitManager(tmp_path / "state", run=fake).observe(unit)
    assert path.read_bytes() == before


def test_different_launch_nonce_is_not_adopted(tmp_path):
    fake = FakeSystemd()
    manager = JobUnitManager(tmp_path / "state", run=fake)
    unit = manager.start(spec(tmp_path))
    fake.description = "SkyBuild launch " + "b" * 32
    with pytest.raises(JobUnitError, match="differs"):
        manager.observe(unit)


def test_interrupted_identity_persistence_cannot_adopt_later_replacement(tmp_path, monkeypatch):
    fake = FakeSystemd()
    manager = JobUnitManager(tmp_path / "state", run=fake)
    job = spec(tmp_path)
    def interrupted(*_args):
        raise OSError("injected persistence failure")
    monkeypatch.setattr(manager, "_write_manifest", interrupted)
    with pytest.raises(OSError, match="persistence failure"):
        manager.start(job)
    fake.invocation = "b" * 32
    recovered = JobUnitManager(tmp_path / "state", run=fake)
    with pytest.raises(JobUnitError, match="unpinned"):
        recovered.observe(job.unit())
    with pytest.raises(JobUnitError, match="already exists"):
        recovered.start(job)


def test_stop_remains_disabled_even_for_matching_invocation(tmp_path):
    fake = FakeSystemd()
    manager = JobUnitManager(tmp_path / "state", run=fake)
    unit = manager.start(spec(tmp_path))
    fake.calls.clear()
    with pytest.raises(JobUnitError, match="cannot atomically guard"):
        manager.stop(unit)
    assert fake.calls == []


def test_legacy_unpinned_manifest_requires_reconciliation(tmp_path):
    fake = FakeSystemd()
    manager = JobUnitManager(tmp_path / "state", run=fake)
    unit = manager.start(spec(tmp_path))
    path = tmp_path / "state" / f"{unit}.json"
    manifest = json.loads(path.read_text())
    del manifest["invocation_id"]
    del manifest["launch_nonce"]
    path.write_text(json.dumps(manifest))
    before = path.read_bytes()
    with pytest.raises(JobUnitError, match="unpinned"):
        manager.observe(unit)
    assert path.read_bytes() == before


@pytest.mark.parametrize("invocation", ["", "0" * 32, "not-an-invocation"])
def test_start_cannot_pin_missing_or_invalid_identity(tmp_path, invocation):
    fake = FakeSystemd()
    fake.invocation = invocation
    manager = JobUnitManager(tmp_path / "state", run=fake)
    job = spec(tmp_path)
    with pytest.raises(JobUnitError, match="unpinned"):
        manager.start(job)
    manifest = json.loads((tmp_path / "state" / f"{job.unit()}.json").read_text())
    assert manifest["invocation_id"] is None
    assert manifest["phase"] == "launch_intent"
