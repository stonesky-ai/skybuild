"""Focused checks for durable, conservative cgroup capacity guidance."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from host_watch import write_state
from memory_capacity import observe


GIB = 1024**3


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


def test_completed_peak_persists_and_target_rises_one_step(tmp_path):
    root = tmp_path / "cgroups"
    root.mkdir()
    registry = tmp_path / "jobs"
    registry.mkdir()
    state_path = tmp_path / "capacity.json"
    (registry / "job-one.json").write_text(json.dumps({
        "unit": "job-one", "phase": "completed", "control_group": "",
        "memory_peak_bytes": 2 * GIB, "memory_max_bytes": 4 * GIB,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }), encoding="utf-8")
    targets = []
    for _ in range(10):
        state = observe(registry, state_path, available_bytes=24 * GIB,
                        reserve_bytes=8 * GIB, new_job_limit_bytes=4 * GIB,
                        cgroup_root=root, hard_concurrency=10, step_samples=5)
        targets.append(state["target_jobs"])
        write_state(state_path, state)
    assert targets == [1, 1, 1, 1, 2, 2, 2, 2, 2, 3]
    assert state["measured_peak_bytes"] == 2 * GIB
    assert state["measurement_count"] == 1
    assert state["peak_history"][0]["source"] == "systemd-MemoryPeak"
    assert state["measurement_age_seconds"] < 10


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
    group = _group(root, "finished", 0, 5 * GIB, 6 * GIB)
    registry = tmp_path / "jobs"
    registry.mkdir()
    _manifest(registry, "job-one", "completed", group)
    state = observe(registry, tmp_path / "capacity.json", available_bytes=30 * GIB,
                    reserve_bytes=8 * GIB, new_job_limit_bytes=4 * GIB, cgroup_root=root)
    assert state["status"] == "measured_peak_exceeds_new_job_cap"
    assert state["target_jobs"] == 0
    assert state["max_new_jobs"] == 0
