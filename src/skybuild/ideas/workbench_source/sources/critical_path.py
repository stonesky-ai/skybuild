"""Critical path: the timing data directory's gate runs, slow suites, endpoint latency and recommendations.
"""
from __future__ import annotations

import json
import math
import statistics
from pathlib import Path

from ..settings import page_time

# ---------------------------------------------------------------- critical path

TIMING_MAX_AGE_SECONDS = 2 * 60 * 60


def _timing_document(path: Path, now: float) -> dict:
    """Read one recent producer document; never let it break /api/state."""
    try:
        info = path.stat()
    except OSError as exc:
        raise ValueError(f"missing {path.name}: {exc.strerror or exc}") from None
    if now - info.st_mtime > TIMING_MAX_AGE_SECONDS:
        raise ValueError(f"stale {path.name}: older than two hours")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"malformed {path.name}: {exc}") from None
    if not isinstance(data, dict):
        raise TypeError(f"malformed {path.name}: expected an object")
    return data


def _timing_rows(data: dict, *names: str) -> list[dict]:
    for name in names:
        if name in data:
            value = data[name]
            if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
                raise ValueError(f"malformed {name}: expected a list of objects")
            return value
    return []


def _timing_number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def critical_path_section(folder: Path | None, now: float) -> dict:
    """Two optional producer files become one read-only state section."""
    if folder is None:
        return {"state": "unavailable", "reason": "SKYKEEP_TIMING_DATA_DIR unset"}
    empty = {"gate_runs": [], "slow_suites": [], "endpoint_latency": {},
             "recommendations": []}
    try:
        summary = _timing_document(folder / "summary.json", now)
        recs = _timing_document(folder / "recs.json", now)
        runs = _timing_rows(summary, "runs")
        suites = summary["suite_avg"]
        probe = summary["probe"]
        recommendations = _timing_rows(recs, "recs")
        if "runs" not in summary or not isinstance(suites, list) or not isinstance(probe, dict) \
                or not isinstance(probe.get("series"), list):
            raise TypeError("malformed summary.json: suite_avg or probe has wrong shape")
        if "recs" not in recs:
            raise ValueError("malformed recs.json: no recs list")
    except (KeyError, TypeError, ValueError) as exc:
        return {"state": "unavailable", "reason": str(exc), **empty}

    # Producer stores seconds and [name, seconds, count] rows. Present minutes
    # as the reference page does; its source JSON is never passed to HTML.
    gate_runs = []
    for row in runs[:8]:
        critical = _timing_number(row.get("critical_lane_s"))
        even = _timing_number(row.get("ideal_even_s"))
        if critical is None or even is None:
            continue
        shown = {"box": str(row.get("box", "")), "run": str(row.get("run", "")),
                 "critical_minutes": round(critical / 60, 1),
                 "even_split_minutes": round(even / 60, 1)}
        stamp = _timing_number(row.get("mtime"))
        if stamp is not None:
            try:
                shown["when"] = page_time(stamp, "%m-%d %H:%M")
            except (OSError, OverflowError, ValueError):
                pass
        longest = row.get("longest_suite")
        if isinstance(longest, list) and len(longest) >= 2:
            duration = _timing_number(longest[1])
            if duration is not None:
                shown["longest_suite"] = Path(str(longest[0])).name
                shown["longest_suite_minutes"] = round(duration / 60, 1)
        gate_runs.append(shown)
    slow_suites = []
    for row in suites[:10]:
        if not isinstance(row, list) or len(row) < 3:
            continue
        seconds = _timing_number(row[1])
        if seconds is not None:
            slow_suites.append({"suite": Path(str(row[0])).name,
                                "mean_minutes": round(seconds / 60, 1), "runs": row[2]})
    series = []
    for row in probe.get("series", [])[-12:]:
        if not isinstance(row, list) or len(row) < 3:
            continue
        generation, embed = _timing_number(row[1]), _timing_number(row[2])
        if generation is not None and embed is not None:
            series.append({"when": str(row[0]), "generation_seconds": generation,
                           "embed_seconds": embed})
    latency = {"series": series}
    for name, median_key, max_key, series_key in (
        ("generation", "gen_s_med", "gen_s_max", "generation_seconds"),
        ("embed", "embed_s_med", "embed_s_max", "embed_seconds"),
    ):
        values = [row[series_key] for row in series]
        median = _timing_number(probe.get(median_key))
        maximum = _timing_number(probe.get(max_key))
        if median is not None or values:
            latency[f"{name}_median_seconds"] = median if median is not None else round(statistics.median(values), 3)
        if maximum is not None or values:
            latency[f"{name}_max_seconds"] = maximum if maximum is not None else max(values)
    shown_recs = []
    for row in recommendations[:10]:
        shown = {key: row[key] for key in ("title", "where", "confidence", "evidence", "todo_text")
                 if key in row}
        speedup = _timing_number(row.get("speedup_min"))
        if speedup is not None:
            shown["minutes_saved"] = speedup
        shown_recs.append(shown)
    return {"state": "ready", "gate_runs": gate_runs, "slow_suites": slow_suites,
            "endpoint_latency": latency, "recommendations": shown_recs}
