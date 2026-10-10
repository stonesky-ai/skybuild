"""Check admission and failure recovery without a live user service bus."""

import argparse
import json
import subprocess
from pathlib import Path

import pytest

from scripts import managed_session as session


def arguments(tmp_path, **changes):
    values = dict(profile="merge", expected_host="wonko", checkout=tmp_path,
                  state_dir=tmp_path / "state", target_ref="refs/heads/dev-006",
                  memory_high_gib=3, memory_max_gib=4, runtime_seconds=120,
                  command=["/usr/bin/true", "refs/heads/dev-006"])
    values.update(changes)
    return argparse.Namespace(**values)


def setup_run(monkeypatch):
    monkeypatch.setattr(session, "validate", lambda args: {})
    monkeypatch.setattr(session, "available_memory", lambda: 24 * session.GIB)


def test_unknown_launch_keeps_merge_slot_and_refuses_replay(tmp_path, monkeypatch):
    setup_run(monkeypatch)
    calls = []

    def lost_reply(argv, **kwargs):
        calls.append(argv)
        raise OSError("The launch reply was lost")

    monkeypatch.setattr(session.subprocess, "run", lost_reply)
    args = arguments(tmp_path)
    with pytest.raises(OSError, match="reply was lost"):
        session.run(args)
    assert (args.state_dir / "merge-slot.json").is_file()
    with pytest.raises(session.SessionError, match="slot is occupied"):
        session.run(args)
    assert len(calls) == 1


def test_terminal_exit_requires_cleanup_evidence_before_slot_release(tmp_path, monkeypatch):
    setup_run(monkeypatch)
    monkeypatch.setattr(session.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 0))
    monkeypatch.setattr(session, "checked", lambda *a, **k: "LoadState=not-found\nActiveState=inactive")
    args = arguments(tmp_path)
    with pytest.raises(FileNotFoundError):
        session.run(args)
    assert (args.state_dir / "merge-slot.json").is_file()


@pytest.mark.parametrize("exit_code", [0, 1, 137])
def test_confirmed_cleanup_releases_slot_after_success_or_failure(tmp_path, monkeypatch, exit_code):
    setup_run(monkeypatch)
    args = arguments(tmp_path)

    def service(argv, **kwargs):
        unit = next(value.split("=", 1)[1] for value in argv if value.startswith("--unit="))
        session.write_new(args.state_dir / f"{unit}.result.json", {"unit": unit, "cleanup_confirmed": True})
        return subprocess.CompletedProcess(argv, exit_code)

    monkeypatch.setattr(session.subprocess, "run", service)
    monkeypatch.setattr(session, "checked", lambda *a, **k: "LoadState=not-found\nActiveState=inactive")
    assert session.run(args) == exit_code
    assert not (args.state_dir / "merge-slot.json").exists()


def test_active_memory_exposure_is_reserved(tmp_path, monkeypatch):
    setup_run(monkeypatch)
    args = arguments(tmp_path, profile="analysis", target_ref=None)
    session.private_directory(args.state_dir)
    session.write_new(args.state_dir / "skybuild-analysis-old.service.json",
                      {"unit": "skybuild-analysis-old.service", "memory_max_bytes": 4 * session.GIB})
    monkeypatch.setattr(session, "available_memory", lambda: 15 * session.GIB)
    with pytest.raises(session.SessionError, match="reserve plus"):
        session.run(args)
    assert len(list(args.state_dir.glob("skybuild-*.service.json"))) == 1


def test_unconfirmed_cleanup_does_not_release_memory_exposure(tmp_path, monkeypatch):
    setup_run(monkeypatch)
    args = arguments(tmp_path, profile="analysis", target_ref=None)
    session.private_directory(args.state_dir)
    unit = "skybuild-analysis-old.service"
    session.write_new(args.state_dir / f"{unit}.json", {"unit": unit, "memory_max_bytes": 4 * session.GIB})
    session.write_new(args.state_dir / f"{unit}.result.json", {"unit": unit, "cleanup_confirmed": False})
    monkeypatch.setattr(session, "available_memory", lambda: 15 * session.GIB)
    with pytest.raises(session.SessionError, match="reserve plus"):
        session.run(args)


def test_wrong_host_stops_before_git_or_service_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(session.socket, "gethostname", lambda: "Jeltz")
    monkeypatch.setattr(session, "checked", lambda *a, **k: pytest.fail("A command ran on the wrong host"))
    with pytest.raises(session.SessionError, match="execution host"):
        session.validate(arguments(tmp_path))


def test_private_state_rejects_shared_directory(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o755)
    with pytest.raises(session.SessionError, match="mode 0700"):
        session.private_directory(state)


def test_service_keeps_arguments_and_stops_all_children(tmp_path):
    args = arguments(tmp_path, command=["/usr/bin/printf", "%s", "refs/heads/dev-006"])
    argv = session.service_command(args, "skybuild-merge-test.service")
    assert argv[-3:] == args.command
    assert argv.index("--") > argv.index("--property")
    assert {"KillMode=control-group", "RemainAfterExit=no", "OOMPolicy=kill",
            "MemoryHigh=3221225472", "MemoryMax=4294967296", "MemorySwapMax=0"} <= set(argv)
    assert any(value.startswith("ExecStopPost=") for value in argv)
    assert not any(value.startswith("GH_TOKEN=") for value in argv)
