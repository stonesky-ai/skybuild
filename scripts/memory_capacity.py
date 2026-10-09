"""Read managed cgroup memory and persist conservative worker capacity guidance.

This module observes a launcher-owned registry. It never starts, stops, or changes a job.
The launcher must keep a completed job's cgroup until its peak has been sampled.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
_GIB = 1024**3
_MEASUREMENT_MAX_AGE_SECONDS = 24 * 60 * 60


class _MemoryCapMismatch(ValueError):
    """Observed, declared or prior hard caps do not match this configuration."""


def _number(path: Path) -> int:
    value = path.read_text(encoding="ascii").strip()
    if not value.isdecimal():
        raise ValueError(f"Expected finite cgroup memory counter: {path.name}")
    return int(value)


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Memory measurement time lacks timezone")
    return parsed


def _registry(path: Path, cgroup_root: Path) -> list[dict]:
    if path.is_dir():
        files = sorted(path.glob("*.json"))
        if len(files) > 64:
            raise ValueError("Job registry exceeds 64 entries")
        entries = [json.loads(file.read_text(encoding="utf-8")) for file in files]
    else:
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or not isinstance(document.get("jobs"), list):
            raise ValueError("Job registry must contain a jobs array")
        entries = document["jobs"]
    if len(entries) > 64:
        raise ValueError("Job registry exceeds 64 entries")
    root = cgroup_root.resolve(strict=True)
    jobs = []
    seen = set()
    for item in entries:
        if not isinstance(item, dict):
            raise ValueError("Job registry entry must be an object")
        job_id, phase = item.get("job_id", item.get("unit")), item.get("phase")
        raw_path = item.get("cgroup")
        if raw_path is None and isinstance(item.get("control_group"), str) and item["control_group"]:
            raw_path = str(root / item["control_group"].lstrip("/"))
        if not isinstance(job_id, str) or not _ID.fullmatch(job_id) or job_id in seen:
            raise ValueError("Job ID is invalid or duplicated")
        if phase not in {"running", "completed", "failed", "interrupted"}:
            raise ValueError("Job phase or cgroup path is invalid")
        if raw_path:
            if not isinstance(raw_path, str):
                raise ValueError("Cgroup path must be text")
            cgroup = Path(raw_path)
            if not cgroup.is_absolute():
                raise ValueError("Cgroup path must be absolute")
            cgroup = cgroup.resolve(strict=phase == "running")
            if cgroup == root or root not in cgroup.parents:
                raise ValueError("Cgroup path escapes configured root")
        elif phase == "running":
            raise ValueError("Running job has no cgroup path")
        else:
            cgroup = None
        seen.add(job_id)
        jobs.append({"job_id": job_id, "phase": phase, "cgroup": cgroup, "manifest": item})
    return jobs


def observe(registry_path: Path, prior_state_path: Path, *, available_bytes: int,
            reserve_bytes: int, new_job_limit_bytes: int, cgroup_root: Path = Path("/sys/fs/cgroup"),
            hard_concurrency: int = 10, step_samples: int = 5) -> dict:
    """Return guidance; caller atomically writes it before using it for decisions.

    ``MemAvailable`` already includes running jobs' current use. Reserve each job's
    remaining hard cgroup allowance, then reserve a full cap for each new job.
    """
    if min(available_bytes, reserve_bytes, new_job_limit_bytes) < 0 or new_job_limit_bytes == 0:
        raise ValueError("Memory values must be nonnegative and job cap positive")
    if not 1 <= hard_concurrency <= 64 or not 1 <= step_samples <= 60:
        raise ValueError("Concurrency and step interval are out of bounds")
    try:
        prior = json.loads(prior_state_path.read_text(encoding="utf-8"))
        if prior.get("schema_version") != 1:
            prior = {}
    except (OSError, ValueError, AttributeError):
        prior = {}
    now_time = datetime.now(timezone.utc)
    now = now_time.isoformat()
    result = {"schema_version": 1, "sampled_at": now, "source": "cgroup-v2-or-systemd-memory-peak",
              "registry": str(registry_path), "reserve_bytes": reserve_bytes,
              "available_bytes": available_bytes, "new_job_limit_bytes": new_job_limit_bytes,
              "hard_concurrency": hard_concurrency, "status": "unknown", "jobs": [],
              "peak_history": prior.get("peak_history", [])[-64:] if isinstance(prior.get("peak_history"), list) else []}
    try:
        if prior.get("new_job_limit_bytes", new_job_limit_bytes) != new_job_limit_bytes:
            raise _MemoryCapMismatch("Prior capacity configuration has a different cap")
        for measurement in result["peak_history"]:
            if not isinstance(measurement, dict) or measurement.get("max_bytes") != new_job_limit_bytes:
                raise _MemoryCapMismatch("Retained measurement has a different or unknown cap")
        jobs = _registry(registry_path, cgroup_root)
        active_remaining = 0
        running = 0
        recorded_ids = {item["job_id"] for item in result["peak_history"] if isinstance(item, dict) and "job_id" in item}
        new_measurements = 0
        for item in jobs:
            group = item["cgroup"]
            if group is not None and group.exists():
                current = _number(group / "memory.current")
                peak = _number(group / "memory.peak")
                maximum = _number(group / "memory.max")
                source = "cgroup-v2-memory.peak"
                source_time = now
            else:
                manifest = item["manifest"]
                if item["phase"] == "running":
                    raise ValueError("Running cgroup disappeared")
                current = 0
                peak = manifest.get("memory_peak_bytes")
                maximum = manifest.get("memory_max_bytes")
                observed_at = manifest.get("observed_at")
                if not isinstance(peak, int) or not isinstance(maximum, int) or not isinstance(observed_at, str):
                    raise ValueError("Completed job has no observed peak")
                source_datetime = _time(observed_at)
                if source_datetime > now_time:
                    raise ValueError("Completed peak timestamp is invalid")
                source = "systemd-MemoryPeak"
                source_time = observed_at
            if maximum <= 0 or current > maximum or peak < current:
                raise ValueError("Cgroup memory counters are inconsistent")
            manifest_maximum = item["manifest"].get("memory_max_bytes")
            if maximum != new_job_limit_bytes:
                raise _MemoryCapMismatch("Observed MemoryMax differs from configured new-job cap")
            if manifest_maximum is not None and (type(manifest_maximum) is not int or
                                                  manifest_maximum != maximum):
                raise _MemoryCapMismatch("Manifest MemoryMax differs from observed or configured cap")
            record = {"job_id": item["job_id"], "phase": item["phase"],
                      "cgroup": str(group) if group is not None else None,
                      "current_bytes": current, "peak_bytes": peak, "max_bytes": maximum,
                      "source": source}
            result["jobs"].append(record)
            if item["phase"] == "running":
                running += 1
                active_remaining += maximum - current
            elif item["job_id"] not in recorded_ids:
                result["peak_history"].append({"job_id": item["job_id"], "peak_bytes": peak,
                                               "max_bytes": maximum, "observed_at": source_time,
                                               "recorded_at": now,
                                               "source": source})
                recorded_ids.add(item["job_id"])
                new_measurements += 1
        result["peak_history"] = result["peak_history"][-64:]
        peaks = [item["peak_bytes"] for item in result["peak_history"]
                 if isinstance(item, dict) and isinstance(item.get("peak_bytes"), int)]
        prior_lifetime_peak = prior.get("lifetime_peak_bytes")
        if not isinstance(prior_lifetime_peak, int) or prior_lifetime_peak < 0:
            prior_lifetime_peak = 0
        measured_peak = max(peaks + [prior_lifetime_peak]) if peaks or prior_lifetime_peak else None
        result["measured_peak_bytes"] = measured_peak
        result["lifetime_peak_bytes"] = measured_peak
        prior_count = prior.get("measurement_count", 0)
        if not isinstance(prior_count, int) or prior_count < 0:
            prior_count = 0
        result["measurement_count"] = prior_count + new_measurements
        latest = prior.get("latest_measurement_at")
        latest_time = _time(latest) if isinstance(latest, str) else None
        for item in result["peak_history"]:
            if isinstance(item, dict) and isinstance(item.get("observed_at"), str):
                candidate_time = _time(item["observed_at"])
                if latest_time is None or candidate_time > latest_time:
                    latest = item["observed_at"]
                    latest_time = candidate_time
        age = None
        if latest_time is not None:
            if latest_time > now_time:
                raise ValueError("Memory measurement time is in the future")
            age = int((now_time - latest_time).total_seconds())
        result["latest_measurement_at"] = latest
        result["measurement_age_seconds"] = age
        result["running_jobs"] = running
        prior_observed = prior.get("max_observed_running", 0)
        if not isinstance(prior_observed, int) or prior_observed < 0:
            prior_observed = 0
        prior_observed_at = prior.get("max_observed_running_at")
        if isinstance(prior_observed_at, str):
            observed_age = (now_time - _time(prior_observed_at)).total_seconds()
            if observed_age < 0 or observed_age > _MEASUREMENT_MAX_AGE_SECONDS:
                prior_observed = 0
        else:
            prior_observed = 0
        observed_running = max(prior_observed, running, 1 if measured_peak is not None else 0)
        result["max_observed_running"] = observed_running
        result["max_observed_running_at"] = (now if running >= observed_running or
                                             (observed_running == 1 and new_measurements)
                                             else prior_observed_at)
        result["active_remaining_budget_bytes"] = active_remaining
        headroom = available_bytes - reserve_bytes - active_remaining
        result["uncommitted_headroom_bytes"] = headroom
        safe_total = min(hard_concurrency, running + max(0, headroom // new_job_limit_bytes))
        result["safe_total_jobs"] = safe_total
        previous_target = prior.get("target_jobs", 1)
        if not isinstance(previous_target, int) or not 0 <= previous_target <= hard_concurrency:
            previous_target = 1
        previous_healthy = prior.get("healthy_samples", 0)
        if not isinstance(previous_healthy, int) or previous_healthy < 0:
            previous_healthy = 0
        last_counted = prior.get("last_counted_sample_at")
        if isinstance(last_counted, str):
            last_counted_time = _time(last_counted)
            if last_counted_time > now_time:
                raise ValueError("Last counted capacity sample is in the future")
            if (now_time - last_counted_time).total_seconds() > _MEASUREMENT_MAX_AGE_SECONDS:
                last_counted_time = None
                previous_healthy = 0
        else:
            last_counted_time = None
            previous_healthy = 0
        counted_at = None
        cap_covers_peak = measured_peak is not None and measured_peak <= new_job_limit_bytes
        fresh_measurement = cap_covers_peak and age is not None and age <= _MEASUREMENT_MAX_AGE_SECONDS
        if safe_total < previous_target:
            target, healthy = safe_total, 0
        elif measured_peak is not None and not cap_covers_peak:
            target, healthy = 0, 0
        elif not fresh_measurement:
            target, healthy = min(previous_target, 1, safe_total), 0
        elif safe_total > previous_target and previous_target <= observed_running:
            # The watcher samples once per minute. Calls and restarts inside that
            # interval must not manufacture additional healthy observations.
            count_sample = last_counted_time is None or (now_time - last_counted_time).total_seconds() >= 60
            healthy = min(step_samples, previous_healthy + int(count_sample))
            counted_at = now if count_sample else last_counted
            target = previous_target + 1 if healthy >= step_samples else previous_target
            if healthy >= step_samples:
                healthy = 0
        else:
            target, healthy = previous_target, 0
        target = min(target, observed_running + 1)
        result["target_jobs"] = target
        result["healthy_samples"] = healthy
        result["last_counted_sample_at"] = counted_at
        result["max_new_jobs"] = max(0, min(target, safe_total) - running)
        result["status"] = "ok" if fresh_measurement else "needs_completed_measurement"
        if measured_peak is not None and not cap_covers_peak:
            result["status"] = "measured_peak_exceeds_new_job_cap"
        elif cap_covers_peak and not fresh_measurement:
            result["status"] = "measurement_stale"
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        result.update({"status": "unknown", "reason": "memory_cap_mismatch" if isinstance(error, _MemoryCapMismatch)
                       else type(error).__name__,
                       "target_jobs": 0, "max_new_jobs": 0, "healthy_samples": 0,
                       "last_counted_sample_at": None})
    return result
