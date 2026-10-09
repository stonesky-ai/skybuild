"""The gate section: lanes, suites, progress, ETA and notes of one running or finished gate.
"""
from __future__ import annotations

from pathlib import Path

from . import hub as sv

# ---------------------------------------------------------------- the gate

def _out_progress(directory: Path | None, lane: int, plabel: str, suite: str, now: float) -> dict:
    """Percent and age of the last output of a running suite, from its `.out` file."""
    if directory is None:
        return {}
    path = directory / f"lane-p{lane}-{plabel}-{Path(suite).stem}.out"
    try:
        stat = path.stat()
        tail = sv._read(path)[-4000:]
    except OSError:
        return {}
    pct = sv._PROGRESS.findall(tail)
    return {"progress": f"{pct[-1]}%" if pct else "no progress mark yet",
            "last_output": sv.fmt_seconds(now - stat.st_mtime) + " ago",
            "quiet_seconds": int(now - stat.st_mtime)}


def _red_detail(directory: Path | None, lane: int, plabel: str, suite: str) -> str:
    """The failing test ids of a red suite, from its `.out` file; empty when none can be read."""
    if directory is None:
        return ""
    found = sv._FAILED_TEST.findall(sv._read(directory / f"lane-p{lane}-{plabel}-{Path(suite).stem}.out"))
    return ", ".join(f.split("::", 1)[-1] for f in found[:3])


def gate_section(procs: list[dict], state_dirs: list[Path], now: float, label: str | None = None) -> dict | None:
    """The newest gate: running (from `ps`) or finished (from its lane logs, kept on show)."""
    gate = sv.find_gate_run(procs)
    if gate is None and label is not None:
        # A box running a slice has no gate_run.py, only its lane scripts: the oldest of them dates the run.
        ages = [p["elapsed"] for p in procs if "gate_lane.sh" in p["args"] and f" {label}" in p["args"]]
        gate = {"label": label, "elapsed": max(ages, default=0), "pid": 0}
    logs = sv.batch_logs(state_dirs)
    if gate is not None:
        base, running = gate["label"], True
    elif logs:
        base, running = logs[0][2], False
    else:
        return None
    plans = sv.lane_plans(procs, base) if running else {}
    dirs = list(state_dirs)
    for info in plans.values():
        found = sv.lane_state_dir(info["pid"])
        if found is not None and found not in dirs:
            dirs.insert(0, found)
    history = sv.duration_history(dirs, now, base)
    keys = {(p, lane, path.name[len(f"lane-p{lane}-"):-4]) for _, lane, lab, p, path in logs if lab == base} | set(plans)
    started = now - gate["elapsed"] if running else min(
        (m for m, _, lab, _, _ in logs if lab == base), default=now)
    rows, now_doing, reds, warns = [], [], [], []
    remaining_by_lane: dict[tuple[str, int, str], float] = {}
    unknown = overshoot = 0
    took_sum = usual_sum = 0.0
    for phase, lane, plabel in sorted(keys, key=lambda k: (sv.PHASES.index(k[0]), k[1], k[2])):
        directory, text, log_end = None, "", None
        for d in dirs:
            candidate = d / f"lane-p{lane}-{plabel}.log"
            if candidate.is_file():
                directory, text = d, sv._read(candidate)
                try:
                    log_end = candidate.stat().st_mtime
                except OSError:
                    pass
                break
        done = {h["suite"]: h for h in sv.parse_suite_lines(text)}
        info = plans.get((phase, lane, plabel))
        live = info["running"] if info else None
        order = list(info["suites"]) if info else []
        order += [s for s in done if s not in order]
        remaining = 0.0
        for suite in order:
            usual = sv.typical(history, suite)
            row = {"phase": phase, "lane": lane, "suite": suite.removeprefix("tests/"),
                   "usual": sv.fmt_seconds(usual), "runs": sv.history_runs(history, suite),
                   **sv.history_record(history, suite), **({"log_end": log_end} if log_end else {})}
            if suite in done:
                hit = done[suite]
                row.update(state="passed" if hit["rc"] == 0 else "RED", took=sv.fmt_seconds(hit["seconds"]),
                           result=hit["summary"])
                if hit["rc"] != 0:
                    detail = _red_detail(directory, lane, plabel, suite)
                    row["failed"] = detail or "see the lane's .out file"
                    reds.append({"phase": phase, "lane": lane, "suite": suite, "rc": hit["rc"]})
                    warns.append(sv._warn("red", f"{base}:{phase}:{lane}:{suite}",
                                       f"{suite} RED in the {phase} pass on lane {lane} (rc={hit['rc']}): "
                                       f"{row['failed']}"))
                if hit["seconds"] is not None and usual:
                    took_sum += hit["seconds"]
                    usual_sum += usual
            elif live and live["suite"] == suite:
                row.update(state="RUNNING", took=sv.fmt_seconds(live["elapsed"]) + " so far",
                           **_out_progress(directory, lane, plabel, suite, now))
                if usual is None:
                    unknown += 1
                    warns.append(sv._warn("no-history", suite, f"{suite} has no past duration: no ETA for it"))
                elif live["elapsed"] > usual:
                    overshoot += 1
                    row["slow"] = "past its usual time"
                    warns.append(sv._warn("slow", f"{base}:{phase}:{lane}:{suite}",
                                       f"{suite} is past its usual time on lane {lane} ({phase} pass): "
                                       f"{sv.fmt_seconds(live['elapsed'])} against a usual {sv.fmt_seconds(usual)}"))
                else:
                    remaining += usual - live["elapsed"]
                if row.get("quiet_seconds", 0) > sv.QUIET_WARN_SECONDS:
                    warns.append(sv._warn("silent", f"{base}:{phase}:{lane}:{suite}",
                                       f"{suite} on lane {lane} wrote no output for "
                                       f"{sv.fmt_seconds(row['quiet_seconds'])}: it may be stuck"))
                now_doing.append({"phase": phase, "meaning": sv.PHASE_MEANING[phase], "lane": lane,
                                  "suite": suite.removeprefix("tests/"), "elapsed": sv.fmt_seconds(live["elapsed"]),
                                  **{k: row[k] for k in ("progress", "last_output") if k in row}})
            else:
                row["state"] = "pending"
                if usual is None:
                    unknown += 1
                else:
                    remaining += usual
            rows.append(row)
        if info and live is None:
            last = [ln for ln in text.splitlines() if ln.strip()][-1:] or [""]
            now_doing.append({"phase": phase, "meaning": sv.PHASE_MEANING[phase], "lane": lane, "suite": "(between suites: stack bring-up or teardown)",
                              "elapsed": sv.fmt_seconds(info["elapsed"]) + " in this lane pass",
                              "last_line": last[0][:140]})
        if info:
            remaining_by_lane[(phase, lane, plabel)] = remaining
    if running and not plans:
        newest = max(((m, path.name) for m, _, lab, _, path in logs if lab == base), default=None)
        seen = (f"; newest lane log of this batch: {newest[1]}, touched {sv.fmt_seconds(now - newest[0])} ago"
                if newest else "")
        warns.append(sv._warn("gap", base, "gate_run.py is alive but no lane script runs (between passes, "
                                        "teardown, barrier or the verdict)" + seen))
        now_doing.append({"phase": "-", "lane": 0, "elapsed": "-",
                          "suite": "gate_run.py is alive but no lane script runs: between passes "
                                   "(stack teardown, a barrier, or the verdict)" + seen})
    longest = max(remaining_by_lane.values(), default=0.0)
    pace = took_sum / usual_sum if usual_sum >= 30 else None
    prior = sv.earlier_attempt(logs, base, started) if running else None
    rework = [f"{r['suite']} red in the {r['phase']} pass on lane {r['lane']} (rc={r['rc']})" for r in reds]
    if prior:
        rework.append(f"{prior['files']} lane file(s) of {base} predate this run "
                      f"({sv._clock(prior['first'])}-{sv._clock(prior['last'])}): this gate was run before")
        warns.append(sv._warn("repeat", base, rework[-1]))
    notes = []
    if unknown:
        notes.append(f"{unknown} suite(s) have no past duration: the ETA excludes them")
    if overshoot:
        notes.append(f"{overshoot} running suite(s) are past their usual time: the ETA counts them as ending now")
    if pace is not None and pace > 1.3 and running:
        warns.append(sv._warn("pace", base, f"suites are finishing {pace:.1f}x slower than their usual time"))
        notes.append(f"suites are finishing {pace:.1f}x slower than their usual time: at this pace the "
                     f"live phase ends about {sv._clock(now + longest * pace)}")
    if running:
        notes.append("the ETA is the live phase's lane scripts only; a later phase (alone re-runs of the reds) adds to it")
    ended = None if running else max((m for m, _, lab, _, _ in logs if lab == base), default=None)
    named = sv._BATCH_OF_LABEL.match(base)
    return {"label": base, "state": "running" if running else "finished",
            # The lanes torn down for this batch (by the integrator or by gate_run): steps of the batch.
            "teardowns": sv.teardown_runs(dirs, int(named.group(1)) if named else None),
            "started": sv._clock(started), "started_ts": started if running else None,
            # Numbers, both states: what ranks gates across boxes and dates one against the ledger.
            "started_at": started, "ended_at": ended,
            "running_for": sv.fmt_seconds(gate["elapsed"]) if running else "-",
            "now": now_doing,
            "pace_vs_usual": f"{pace:.1f}x" if pace is not None else "unknown",
            "eta_live_phase": sv.fmt_seconds(longest) if running and remaining_by_lane else "-",
            "finishes_about": sv._clock(now + longest) if running and remaining_by_lane else "-",
            "rows": rows, "rework": rework, "notes": notes, "warnings": warns,
            "live_passes": [{"phase": ph, "lane": ln, "label": lab, "started": now - plans[(ph, ln, lab)]["elapsed"]}
                            for ph, ln, lab in sorted(plans, key=lambda k: (sv.PHASES.index(k[0]), k[1]))]}
