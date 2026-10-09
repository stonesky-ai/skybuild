"""Focused checks for durable, conservative cgroup capacity guidance."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from host_watch import write_state
from memory_capacity import observe
import memory_capacity


GIB = 1024**3


@pytest.fixture
def clock(monkeypatch):
    class Clock(datetime):
        current = datetime.now(timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls.current

        @classmethod
        def advance(cls, seconds):
            cls.current += timedelta(seconds=seconds)

    monkeypatch.setattr(memory_capacity, "datetime", Clock)
    return Clock


def _group(root: Path, name: str, current: int, peak: int, maximum: int) -> Path:
    group = root / name
    group.mkdir()
    for filename, value in (("memory.current", current), ("memory.peak", peak), ("memory.max", maximum)):
        (group / filename).write_text(str(value), encoding="ascii")
    return group


def _manifest(directory: Path, unit: str, phase: str, group: Path, **extra) -> None:
    (directory / f"{unit}.json").write_text(json.dumps({
        "unit": unit, "phase": phase, "cgroup": str(group), **extra
    }), encoding="utf-8")


def test_reserves_running_cgroup_remainder_before_new_job(tmp_path):
    root = tmp_path / "cgroups"
    root.mkdir()
    group = _group(root, "one", GIB, 2 * GIB, 4 * GIB)
    registry = tmp_path / "jobs"
    registry.mkdir()
    (registry / "job-one.json").write_text(json.dumps({
        "unit": "job-one", "phase": "running", "control_group": "/one",
        "memory_current_bytes": GIB, "memory_peak_bytes": 2 * GIB,
        "memory_max_bytes": 4 * GIB,
    }), encoding="utf-8")
    state = observe(registry, tmp_path / "capacity.json", available_bytes=13 * GIB,
                    reserve_bytes=8 * GIB, new_job_limit_bytes=4 * GIB, cgroup_root=root)
    assert state["active_remaining_budget_bytes"] == 3 * GIB
    assert state["safe_total_jobs"] == 1
    assert state["max_new_jobs"] == 0
    assert state["status"] == "needs_completed_measurement"


def test_completed_peak_persists_and_target_rises_one_step(tmp_path, clock):
    root = tmp_path / "cgroups"
    root.mkdir()
    registry = tmp_path / "jobs"
    registry.mkdir()
    state_path = tmp_path / "capacity.json"
    (registry / "job-one.json").write_text(json.dumps({
        "unit": "job-one", "phase": "completed", "control_group": "",
        "memory_peak_bytes": 2 * GIB, "memory_max_bytes": 4 * GIB,
        "observed_at": clock.now(timezone.utc).isoformat(),
    }), encoding="utf-8")
    targets = []
    for _ in range(10):
        state = observe(registry, state_path, available_bytes=24 * GIB,
                        reserve_bytes=8 * GIB, new_job_limit_bytes=4 * GIB,
                        cgroup_root=root, hard_concurrency=10, step_samples=5)
        targets.append(state["target_jobs"])
        write_state(state_path, state)
        clock.advance(60)
    assert targets == [1, 1, 1, 1, 2, 2, 2, 2, 2, 2]
    assert state["max_observed_running"] == 1
    assert state["measured_peak_bytes"] == 2 * GIB
    assert state["measurement_count"] == 1
    assert state["peak_history"][0]["source"] == "systemd-MemoryPeak"
    assert state["measurement_age_seconds"] == 9 * 60
    for name in ("two", "three"):
        group = _group(root, name, 2 * GIB, 2 * GIB, 4 * GIB)
        _manifest(registry, f"job-{name}", "running", group)
    for _ in range(5):
        state = observe(registry, state_path, available_bytes=24 * GIB,
                        reserve_bytes=8 * GIB, new_job_limit_bytes=4 * GIB,
                        cgroup_root=root, hard_concurrency=10, step_samples=5)
        write_state(state_path, state)
        clock.advance(60)
    assert state["max_observed_running"] == 2
    assert state["target_jobs"] == 3
    assert state["max_new_jobs"] == 1


def test_unknown_launch_intent_blocks_admission(tmp_path):
    root = tmp_path / "cgroups"
    root.mkdir()
    registry = tmp_path / "jobs"
    registry.mkdir()
    _manifest(registry, "job-one", "launch_intent", root / "missing")
    state = observe(registry, tmp_path / "capacity.json", available_bytes=30 * GIB,
                    reserve_bytes=8 * GIB, new_job_limit_bytes=4 * GIB, cgroup_root=root)
    assert state["status"] == "unknown"
    assert state["max_new_jobs"] == 0


def test_measured_peak_over_cap_blocks_ramp(tmp_path):
    root = tmp_path / "cgroups"
    root.mkdir()
    group = _group(root, "finished", 0, 5 * GIB, 4 * GIB)
    registry = tmp_path / "jobs"
    registry.mkdir()
    _manifest(registry, "job-one", "completed", group)
    state = observe(registry, tmp_path / "capacity.json", available_bytes=30 * GIB,
                    reserve_bytes=8 * GIB, new_job_limit_bytes=4 * GIB, cgroup_root=root)
    assert state["status"] == "measured_peak_exceeds_new_job_cap"
    assert state["target_jobs"] == 0
    assert state["max_new_jobs"] == 0


def test_stale_observed_concurrency_cannot_keep_large_target(tmp_path):
    root = tmp_path / "cgroups"
    root.mkdir()
    registry = tmp_path / "jobs"
    registry.mkdir()
    state_path = tmp_path / "capacity.json"
    write_state(state_path, {
        "schema_version": 1, "target_jobs": 10, "healthy_samples": 0,
        "lifetime_peak_bytes": 2 * GIB, "measurement_count": 1,
        "latest_measurement_at": datetime.now(timezone.utc).isoformat(),
        "max_observed_running": 10,
        "max_observed_running_at": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
    })
    state = observe(registry, state_path, available_bytes=50 * GIB,
                    reserve_bytes=8 * GIB, new_job_limit_bytes=4 * GIB, cgroup_root=root)
    assert state["max_observed_running"] == 1
    assert state["target_jobs"] == 2


def _completed_registry(tmp_path, observed_at, maximum=4 * GIB):
    root = tmp_path / "cgroups"
    root.mkdir()
    registry = tmp_path / "jobs"
    registry.mkdir()
    (registry / "job-one.json").write_text(json.dumps({
        "unit": "job-one", "phase": "completed", "control_group": "",
        "memory_peak_bytes": 2 * GIB, "memory_max_bytes": maximum,
        "observed_at": observed_at,
    }))
    return registry, root, tmp_path / "capacity.json"


def _sample(registry, root, path):
    state = observe(registry, path, available_bytes=30 * GIB,
                    reserve_bytes=8 * GIB, new_job_limit_bytes=4 * GIB,
                    cgroup_root=root, step_samples=5)
    write_state(path, state)
    return state


def test_rapid_repeats_and_restart_cannot_manufacture_minutes(tmp_path, clock):
    registry, root, path = _completed_registry(tmp_path, clock.now().isoformat())
    first = _sample(registry, root, path)
    counted_at = first.get("last_counted_sample_at")
    for _ in range(10):
        clock.advance(5)
        state = _sample(registry, root, path)
        assert state["target_jobs"] == 1 and state["healthy_samples"] == 1
        assert state["last_counted_sample_at"] == counted_at
    # Every call reloads the atomically persisted state, including after restart.
    clock.advance(9)
    assert _sample(registry, root, path)["healthy_samples"] == 1
    clock.advance(1)
    assert _sample(registry, root, path)["healthy_samples"] == 2
    for expected in (3, 4):
        clock.advance(60)
        state = _sample(registry, root, path)
        assert state["healthy_samples"] == expected and state["target_jobs"] == 1
    clock.advance(60)
    state = _sample(registry, root, path)
    assert state["target_jobs"] == 2 and state["healthy_samples"] == 0


def test_clock_rollback_blocks_guidance_and_clears_streak(tmp_path, clock):
    registry, root, path = _completed_registry(tmp_path, (clock.now() - timedelta(minutes=1)).isoformat())
    _sample(registry, root, path)
    clock.advance(-1)
    state = _sample(registry, root, path)
    assert state["status"] == "unknown" and state["max_new_jobs"] == 0
    assert state["healthy_samples"] == 0 and state["last_counted_sample_at"] is None


def test_stale_measurement_cannot_continue_healthy_streak(tmp_path, clock):
    registry, root, path = _completed_registry(tmp_path, clock.now().isoformat())
    for _ in range(4):
        state = _sample(registry, root, path)
        clock.advance(60)
    assert state["healthy_samples"] == 4
    clock.advance(24 * 60 * 60)
    state = _sample(registry, root, path)
    assert state["status"] == "measurement_stale"
    assert state["target_jobs"] == 1 and state["healthy_samples"] == 0
    assert state["last_counted_sample_at"] is None


def test_stale_counted_sample_cannot_reuse_streak_with_fresh_peak(tmp_path, clock):
    registry, root, path = _completed_registry(tmp_path, clock.now().isoformat())
    for _ in range(4):
        _sample(registry, root, path)
        clock.advance(60)
    clock.advance(24 * 60 * 60)
    (registry / "job-two.json").write_text(json.dumps({
        "unit": "job-two", "phase": "completed", "memory_peak_bytes": GIB,
        "memory_max_bytes": 4 * GIB, "observed_at": clock.now().isoformat(),
    }))
    state = _sample(registry, root, path)
    assert state["status"] == "ok" and state["target_jobs"] == 1
    assert state["healthy_samples"] == 1


@pytest.mark.parametrize("phase", ["running", "completed"])
@pytest.mark.parametrize("actual,manifest", [(8, 8), (8, 4), (4, 8)])
def test_live_or_declared_cap_mismatch_blocks_even_small_peaks(tmp_path, clock, phase, actual, manifest):
    root = tmp_path / "cgroups"
    root.mkdir()
    group = _group(root, "one", GIB if phase == "running" else 0, 2 * GIB, actual * GIB)
    registry = tmp_path / "jobs"
    registry.mkdir()
    _manifest(registry, "job-one", phase, group, memory_max_bytes=manifest * GIB)
    state = _sample(registry, root, tmp_path / "capacity.json")
    assert state["status"] == "unknown" and state["reason"] == "memory_cap_mismatch"
    assert state["target_jobs"] == 0 and state["max_new_jobs"] == 0


def test_completed_retained_manifest_cap_mismatch_blocks(tmp_path, clock):
    registry, root, path = _completed_registry(tmp_path, clock.now().isoformat(), maximum=8 * GIB)
    state = _sample(registry, root, path)
    assert state["status"] == "unknown" and state["reason"] == "memory_cap_mismatch"
    assert state["max_new_jobs"] == 0


@pytest.mark.parametrize("old_cap,history_cap", [(8, 4), (4, 8)])
def test_prior_configuration_or_peak_history_cap_mismatch_blocks(tmp_path, clock, old_cap, history_cap):
    registry, root, path = _completed_registry(tmp_path, clock.now().isoformat())
    write_state(path, {"schema_version": 1, "new_job_limit_bytes": old_cap * GIB,
                       "target_jobs": 1, "healthy_samples": 4,
                       "last_counted_sample_at": (clock.now() - timedelta(minutes=1)).isoformat(),
                       "peak_history": [{"job_id": "older", "max_bytes": history_cap * GIB,
                                         "peak_bytes": GIB, "observed_at": clock.now().isoformat()}]})
    state = _sample(registry, root, path)
    assert state["status"] == "unknown" and state["reason"] == "memory_cap_mismatch"
    assert state["target_jobs"] == 0 and state["max_new_jobs"] == 0
