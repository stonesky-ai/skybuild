"""Lane logs: the finished suites of a lane, every older log's durations (the typical time of a
suite), and the record of each suite's runs.
"""
from __future__ import annotations

import re
import statistics
from collections.abc import Mapping
from pathlib import Path

from . import hub as sv

PHASES = ("wave", "restarts", "alone", "rerun")
_PHASE_SUFFIX = {"": "wave", "-restarts": "restarts", "-alone": "alone"}
#: What each pass of the gate is for, said on the page beside the lane doing it.
PHASE_MEANING = {
    "wave": "the main suite pass, lanes in parallel",
    "restarts": "suites that restart containers, run apart from the wave",
    "alone": "red suites re-run alone on one lane",
    "rerun": "one red suite re-run by itself to see whether the red reproduces",
}
_RERUN = re.compile(r"-rerun-.+$")
#: Runs of one suite the hover list shows; the usual time is the median of the green ones.
SHOWN_RUNS = 10
_PROGRESS = re.compile(r"\[\s*(\d+)%\]")
_FAILED_TEST = re.compile(r"^FAILED\s+(\S+)", re.MULTILINE)
_LANE_LOG = re.compile(r"^lane-p(\d+)-(.+?)(-restarts|-alone|-rerun-.+)?\.log$")
#: A lane label that only tears lanes down: the integrator's `teardown<N>` (stacks taken down to make room for
#: an alone run of batch N) and gate_run's own `<label>-rerun` before its re-run pass. Neither is a gate or a
#: batch of its own (owner, 2026-10-07): each is a step of its batch.
_TEARDOWN_LABEL = re.compile(r"^(?:teardown(\d+)|(.+)-rerun)$")
_DOWN_LINE = re.compile(r"^LANE (\S+) DOWN (\S+)(?:\s+\S+\s+(.+?))?\s*$", re.MULTILINE)
_RESET_WHY = re.compile(r"^RESET \S+: (.+?)\s+\S+\s+bringing it down", re.MULTILINE)
_BATCH_OF_LABEL = re.compile(r"^b(?:atch)?(\d+)", re.IGNORECASE)
#: Teardown lane logs read per box, newest first.
TEARDOWNS_READ = 60


def is_teardown_label(label: str) -> bool:
    return bool(_TEARDOWN_LABEL.match(label or ""))


def teardown_batch(label: str) -> int | None:
    """The batch a teardown label belongs to: `teardown92` -> 92, `batch92e-rerun` -> 92; None for another label."""
    hit = _TEARDOWN_LABEL.match(label or "")
    if not hit:
        return None
    if hit.group(1):
        return int(hit.group(1))
    named = _BATCH_OF_LABEL.match(hit.group(2))
    return int(named.group(1)) if named else None


def _births(paths: list[Path]) -> dict[str, float]:
    """When each file was created, where the filesystem keeps it (`stat -c %W`); none where it does not."""
    out: dict[str, float] = {}
    for path in paths:
        born = getattr(_stat_or_none(path), "st_birthtime", None)
        if born:
            out[str(path)] = float(born)
    missing = [str(p) for p in paths if str(p) not in out]
    if missing:
        import subprocess
        try:
            done = subprocess.run(["stat", "-c", "%W %n", "--", *missing], capture_output=True, text=True,
                                  timeout=5, check=False)
            for line in done.stdout.splitlines():
                stamp, _, name = line.partition(" ")
                if stamp.isdigit() and int(stamp) > 0:
                    out[name] = float(stamp)
        except (OSError, subprocess.SubprocessError):
            pass
    return out


def _stat_or_none(path: Path):
    try:
        return path.stat()
    except OSError:
        return None


def teardown_runs(state_dirs: list[Path], batch: int | None) -> list[dict]:
    """The teardown lane logs of batch `batch`, one per lane: {lane, label, start, end, reason, ok}.

    `reason` is what the log itself says (the lane's `LANE <project> DOWN <ts> — <why>` line, and the reset's
    `RESET <project>: <why> — bringing it down` line), or "" when it says nothing.
    """
    if batch is None:
        return []
    found: list[tuple[float, int, str, Path]] = []
    for directory in state_dirs:
        try:
            for path in directory.glob("lane-p*.log"):
                hit = _LANE_LOG.match(path.name)
                label = path.name[len(f"lane-p{hit.group(1)}-"):-4] if hit else ""
                if hit and teardown_batch(label) == batch:
                    stat = _stat_or_none(path)
                    if stat is not None:
                        found.append((stat.st_mtime, int(hit.group(1)), label, path))
        except OSError:
            continue
    found = sorted(found, reverse=True)[:TEARDOWNS_READ]
    births = _births([path for _, _, _, path in found])
    out = []
    for mtime, lane, label, path in sorted(found):
        text = _read(path)
        down = _DOWN_LINE.search(text)
        why = _RESET_WHY.search(text)
        reason = "; ".join(x for x in ((down.group(3) or "") if down else "", why.group(1) if why else "") if x)
        out.append({"lane": lane, "label": label, "start": births.get(str(path)), "end": mtime,
                    "reason": reason[:200], "ok": "COMPOSE DOWN -v EXIT=0" in text or (down is not None)})
    return out


# ---------------------------------------------------------------- lane logs

def parse_suite_lines(text: str) -> list[dict]:
    """Every `SUITE <path> rc=<n> <summary>` line: {suite, rc, seconds, summary}."""
    out = []
    for line in text.splitlines():
        hit = sv._SUITE_LINE.match(line.strip())
        if not hit:
            continue
        dur = sv._DURATION.search(hit.group(3))
        out.append({"suite": hit.group(1), "rc": int(hit.group(2)),
                    "seconds": float(dur.group(1)) if dur else None,
                    "summary": hit.group(3)[:160]})
    return out


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def duration_history(state_dirs: list[Path], now: float, skip_label: str = "") -> dict[str, list[dict]]:
    """suite -> its newest runs [{at, seconds, rc}], oldest first, every lane log of every batch. Cached.

    The running batch's own logs are skipped: a run is not its own yardstick.
    """
    key = (tuple(sorted(str(d) for d in state_dirs)), skip_label)
    if sv._history_cache["key"] == key and now - sv._history_cache["at"] < sv.HISTORY_TTL_SECONDS:
        return sv._history_cache["value"]
    logs: list[tuple[float, Path]] = []
    for directory in state_dirs:
        try:
            for path in directory.glob("lane-p*.log"):
                if skip_label and f"-{skip_label}" in path.name:
                    continue
                try:
                    logs.append((path.stat().st_mtime, path))
                except OSError:
                    continue
        except OSError:
            continue
    history: dict[str, list[dict]] = {}
    for mtime, path in sorted(logs):
        for hit in parse_suite_lines(_read(path)):
            if hit["seconds"] is not None:
                history.setdefault(hit["suite"], []).append(
                    {"at": mtime, "seconds": hit["seconds"], "rc": hit["rc"], "batch": _batch_of(path.name)})
    value = {s: v[-SHOWN_RUNS:] for s, v in history.items()}
    sv._history_cache.update(at=now, key=key, value=value)
    return value


def _batch_of(name: str) -> str:
    hit = _LANE_LOG.match(name)
    return hit.group(2) if hit else name


def typical(history: Mapping[str, list[dict]], suite: str) -> float | None:
    green = [r["seconds"] for r in history.get(suite, []) if r["rc"] == 0]
    return statistics.median(green) if green else None


def history_runs(history: Mapping[str, list[dict]], suite: str) -> list[dict]:
    """The popup's lines, newest first: one per earlier run, with its result and batch."""
    return [{"when": sv.zoned(r["at"], "%m-%d %H:%M"), "took": sv.fmt_seconds(r["seconds"]),
             "result": "passed" if r["rc"] == 0 else f"RED rc={r['rc']}", "batch": r["batch"]}
            for r in reversed(history.get(suite, []))]


def history_record(history: Mapping[str, list[dict]], suite: str) -> dict:
    """Pass/fail over the shown runs: a strip (oldest first), a rate and the current red streak."""
    runs = history.get(suite, [])
    marks = "".join("P" if r["rc"] == 0 else "F" for r in runs)
    streak = len(marks) - len(marks.rstrip("F"))
    out = {"strip": marks, "record": f"{marks.count('P')}/{len(marks)} passed" if marks else "no runs"}
    if streak >= 2:
        out["streak"] = f"red {streak} runs in a row"
    elif marks.count("F") >= 3:
        out["streak"] = f"red {marks.count('F')} of the last {len(marks)}"
    return out


def batch_logs(state_dirs: list[Path]) -> list[tuple[float, int, str, str, Path]]:
    """Every lane log as (mtime, lane, batch label, phase, path), newest first."""
    out = []
    for directory in state_dirs:
        try:
            for path in directory.glob("lane-p*.log"):
                hit = _LANE_LOG.match(path.name)
                if hit and not is_teardown_label(hit.group(2)):
                    out.append((path.stat().st_mtime, int(hit.group(1)), hit.group(2),
                                _PHASE_SUFFIX.get(hit.group(3) or "", "rerun"), path))
        except OSError:
            continue
    return sorted(out, reverse=True)


def earlier_attempt(logs: list[tuple[float, int, str, str, Path]], label: str,
                    run_started: float) -> dict | None:
    """Lane files for this batch label older than the running gate: a run that was repeated."""
    stamps = [m for m, _, lab, _, _ in logs if lab == label and m < run_started - 60]
    return {"files": len(stamps), "first": min(stamps), "last": max(stamps)} if stamps else None
