"""Integration now: every step of one integration run, in order, with its state, its server and its history.

The order is the integrator's own: its role brief (`.claude/skills/overseer/roles/integrator.md`), then
`seam_bundle.py check`, `seam_merge.py`, `skykeep.sh testfast`, `gate_run.py`'s passes (preflight, the lanes'
bring-up behind the barrier, the wave pass, the restart wave, the clamd wave, the alone phase, the re-run pass,
the verdict), and `seam_land.py`'s operations (its preflights, the brief deletions, the MasterToDo render and
check, the one ledger commit, the fast-forward push, the branch deletions), then the todo service's facts and
the integrator's exit. Owner, 2026-10-07: every step listed, not only the gates, with progress and the earlier
runs' times and outcomes, so what comes next can be expected.

Each step's state is read from what the page already sees (the raw steps `build_steps` reads from the trunk,
the fast-test logs and the gate), from the integrator's own status log (`SESSIONVIEW_WEB_INTEGRATOR_LOG`, its
`ts | role | status | text | next` lines), and from the gate reports kept as history. A step nothing records
is never shown done by guess: once a later step has begun it says "no record".

History: every finished gate report the page reads (here, and pulled over ssh from the gate boxes every
`HISTORY_PULL_SECONDS`) is kept as one record per run in `run-history.json` beside the steps file
(`SESSIONVIEW_WEB_RUN_HISTORY_FILE` overrides); the integrator log gives the times of its other steps. A step's
expected time is the median of its earlier durations; with none on record its ETA is `--:--`, never 0.

Not in `hub._ORDER`: this module never runs on another box, so the probe program stays as it was.
"""
from __future__ import annotations

import json
import math
import os
import re
import statistics
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import hub as sv

# ---------------------------------------------------------------- small pure helpers


def server_abbrev(box: object) -> str:
    """A box's name as the page's three-letter server tag: jeltz -> jel, wowbagger -> wow, aragog -> ara."""
    word = re.sub(r"\(.*?\)", "", str(box or "")).strip().lower()
    return re.sub(r"[^a-z0-9]", "", word)[:3]


def clock_text(seconds: float | None) -> str:
    """Minutes and seconds as `mm:ss` (minutes unbounded: `72:05`); `--:--` when there is no number."""
    if seconds is None or not isinstance(seconds, (int, float)) or not math.isfinite(seconds):
        return "--:--"
    total = round(abs(seconds))
    return f"{total // 60:02d}:{total % 60:02d}"


def eta_text(median: float | None, elapsed: float | None) -> str:
    """`ETA: mm:ss` to the usual end, `overdue mm:ss` once past it, `ETA: --:--` with no history."""
    if not isinstance(median, (int, float)) or not math.isfinite(median):
        return "ETA: --:--"
    spent = elapsed if isinstance(elapsed, (int, float)) and math.isfinite(elapsed) else 0.0
    left = median - spent
    return f"ETA: {clock_text(left)}" if left > -0.5 else f"overdue {clock_text(-left)}"


def median_stats(values: Sequence[tuple[float, bool]]) -> dict | None:
    """{median, runs, ok} of (seconds, succeeded) pairs; None when there are none."""
    kept = [(s, ok) for s, ok in values if isinstance(s, (int, float)) and s >= 0]
    if not kept:
        return None
    return {"median": float(statistics.median(s for s, _ in kept)), "runs": len(kept),
            "ok": sum(1 for _, ok in kept if ok)}


def history_text(stats: Mapping | None) -> str:
    if not stats:
        return "no history"
    return (f"{sv.fmt_seconds(stats['median'])} median, {stats['ok']} of {stats['runs']} ok"
            + (f" ({stats['basis']})" if stats.get("basis") else ""))


_LABEL_BASE = re.compile(r"^b(?:atch)?(\d+)", re.IGNORECASE)


def label_batch(label: object) -> int | None:
    """The batch number a gate label names (`batch92e` -> 92), or None (`gates-2026...` names none)."""
    hit = _LABEL_BASE.match(str(label or ""))
    return int(hit.group(1)) if hit else None


def label_kind(label: object) -> str:
    """`main` for a batch's own gate (`batch92`), `followup` for a later run of it (`batch92e`), else `other`."""
    text = str(label or "")
    if re.fullmatch(r"b(?:atch)?\d+", text, re.IGNORECASE):
        return "main"
    return "followup" if label_batch(text) is not None else "other"


# ---------------------------------------------------------------- gate reports as history

_BARRIER = re.compile(r"^barrier: (\d+) lane\(s\) up and healthy in (\d+)s", re.MULTILINE)
_SCHEDULED = re.compile(r"(\d+) of (\d+) scheduled suite")
_COUNTS = re.compile(r"^(\d+) passed, (\d+) failed(?:, (\d+) UNMEASURED)?", re.MULTILINE)
_RERUN_SKIPPED = re.compile(r"^re-run pass NOT run", re.MULTILINE)
_WRITTEN = re.compile(r"^report written to (\S+)", re.MULTILINE)
_RED_STATES = ("red", "FAILED", "UNMEASURED")
#: The gate's passes in the order gate_run runs them (remote slices run beside them from the start).
GATE_PHASES = ("bringup", "wave", "restarts", "clamd", "alone", "rerun")


def block_phase(block: Mapping) -> str:
    """Which pass one lane block of a gate report belongs to, from its lane log's name."""
    if block.get("remote"):
        return "remote:" + str(block.get("lane", ""))
    name = Path(str(block.get("log", ""))).name
    if "-rerun" in name or "re-run alone" in str(block.get("note", "")):
        return "rerun"
    for suffix, phase in (("-restarts.log", "restarts"), ("-clamd.log", "clamd"), ("-alone.log", "alone"),
                          ("-vision.log", "vision")):
        if name.endswith(suffix):
            return phase
    return "wave"


def gate_record(text: str, *, box: str, path: str, mtime: float | None) -> dict | None:
    """One FINISHED gate run as history, from its console report (and the run's stdout before it, if sent).

    None for a run with no verdict yet, no label, no readable start, or an end before its start.
    """
    if not isinstance(text, str):
        return None
    at = text.rfind("\ngate run ")
    if at < 0 and not text.startswith("gate run "):
        return None
    report = text[at + 1:] if at >= 0 else text
    before = text[:at] if at > 0 else ""
    parsed = sv.parse_gate_log(report)
    if not parsed["label"] or not parsed["blocks"] or not any(sv._VERDICT.match(v) for v in parsed["verdict"]):
        return None
    written = _WRITTEN.search(report)
    started = sv.gate_log_started(Path(written.group(1))) if written else sv.gate_log_started(Path(path))
    if started is None or mtime is None or mtime < started:
        return None
    phases: dict[str, float] = {}
    ok: dict[str, bool] = {}
    reds: set[str] = set()
    cleared: set[str] = set()
    for block in parsed["blocks"]:
        phase = block_phase(block)
        wall = float(block["wall"])
        # The re-run pass runs its suites one after another; every other pass runs its lanes side by side.
        phases[phase] = phases.get(phase, 0.0) + wall if phase == "rerun" else max(phases.get(phase, 0.0), wall)
        ok[phase] = ok.get(phase, True) and block["exit"] == 0
        for suite in block["suites"]:
            name = suite["suite"].rsplit("/", 1)[-1]
            if phase == "rerun" and suite["state"] in ("passed", "did not reproduce"):
                cleared.add(name)
            elif suite["state"] in _RED_STATES:
                reds.add(name)
    barrier = _BARRIER.search(before)
    if barrier:
        phases["bringup"], ok["bringup"] = float(barrier.group(2)), True
    verdict = next((sv._VERDICT.match(v).group(1).split(" ", 2)[-1] for v in parsed["verdict"] if sv._VERDICT.match(v)), "")
    counts = _COUNTS.search(report)
    scheduled = _SCHEDULED.search(report)
    return {"box": box, "label": parsed["label"], "kind": label_kind(parsed["label"]),
            "batch": label_batch(parsed["label"]), "started": float(started), "ended": float(mtime),
            "seconds": float(mtime) - float(started),
            "scheduled": int(scheduled.group(2)) if scheduled else None,
            "passed": int(counts.group(1)) if counts else None, "failed": int(counts.group(2)) if counts else None,
            "unmeasured": int(counts.group(3)) if counts and counts.group(3) else 0,
            "verdict": verdict, "phases": phases, "ok": ok, "reds": sorted(reds - cleared),
            "rerun_skipped": bool(_RERUN_SKIPPED.search(text)), "path": path}


def phase_stats(records: Sequence[Mapping], phase: str, *, kind: str = "main", size: int | None = None,
                last: int = 12) -> dict | None:
    """{median, runs, ok, basis} of one pass over the earlier gate runs of a kind, the similar-sized first.

    A batch gate of 140 suites and a re-run of 4 are different jobs: with `size` known, the runs within half
    to twice that many suites are used when there are at least two of them.
    """
    # A pass that took 0s did not run (a re-run pass refused by its limit): it is no time for the pass.
    chosen = [r for r in records if r.get("kind") == kind and ((r.get("phases") or {}).get(phase) or 0) > 0]
    basis = f"{kind} gate runs" if kind != "main" else "batch gates"
    if size:
        similar = [r for r in chosen if r.get("scheduled") and size / 2 <= r["scheduled"] <= size * 2]
        if len(similar) >= 2:
            chosen, basis = similar, f"runs of {max(1, size // 2)}-{size * 2} suites"
    chosen = sorted(chosen, key=lambda r: r["started"])[-last:]
    got = median_stats([(r["phases"][phase], bool((r.get("ok") or {}).get(phase))) for r in chosen])
    return dict(got, basis=basis) if got else None


def run_stats(records: Sequence[Mapping], *, kind: str, size: int | None = None, last: int = 12) -> dict | None:
    """{median, runs, ok, basis} of whole gate runs of a kind (a follow-up run's own time, for a fixup)."""
    chosen = [r for r in records if r.get("kind") == kind]
    basis = f"{len(chosen)} earlier {kind} runs" if kind != "main" else "batch gates"
    if size:
        similar = [r for r in chosen if r.get("scheduled") and size / 2 <= r["scheduled"] <= size * 2]
        if len(similar) >= 2:
            chosen, basis = similar, f"runs of {max(1, size // 2)}-{size * 2} suites"
    chosen = sorted(chosen, key=lambda r: r["started"])[-last:]
    got = median_stats([(r["seconds"], r.get("verdict") == "GREEN") for r in chosen])
    return dict(got, basis=basis) if got else None


# ---------------------------------------------------------------- the kept history

#: How often the gate boxes' reports are pulled over ssh, how long one pull may take, how many files a box sends.
HISTORY_PULL_SECONDS = 600.0
HISTORY_PULL_TIMEOUT_SECONDS = 45.0
HISTORY_FILES_PER_BOX = 40
#: Bytes read of one report file there, records kept here.
HISTORY_READ_CAP = 400_000
HISTORY_KEPT = 500
RUN_HISTORY_VAR = "SESSIONVIEW_WEB_RUN_HISTORY_FILE"

#: The program a gate box runs for the pull: its report files, newest first, cut to the lines a record reads.
_PULL_PROGRAM = r'''
import glob, json, os, re
KEEP = re.compile(r"^(gate run |lane |  \S|GATE RUN|\d+ passed, |barrier: |re-run pass|report written to |FAILED:|DID NOT REPRODUCE:|\d+ lane\(s\) from|\d+ remote slice|$)")
found = {}
for pattern in PATTERNS:
    if not os.path.isabs(pattern):
        continue
    for path in glob.glob(pattern):
        try:
            found[path] = os.stat(path).st_mtime
        except OSError:
            pass
out = []
for path in sorted(found, key=found.get, reverse=True)[:LIMIT]:
    try:
        with open(path, errors="replace") as handle:
            text = handle.read(CAP)
    except OSError:
        continue
    if "\ngate run " not in text and not text.startswith("gate run "):
        continue
    lines = [line[:240] for line in text.splitlines() if KEEP.match(line)]
    out.append({"path": path, "mtime": found[path], "text": "\n".join(lines)})
print(json.dumps(out))
'''


class RunHistory:
    """Finished gate runs and fast-test runs, kept on disk so a restart forgets nothing; pulls never block a refresh.

    A write or pull that fails is said in `problem` / `pull_problem`, never raised.
    """

    def __init__(self, path: Path | None = None, runner: Callable[..., Any] | None = None) -> None:
        named = os.environ.get(RUN_HISTORY_VAR, "").strip()
        self.path = path or (Path(named) if named else
                             Path.home() / ".local" / "state" / "skykeep-sessionview" / "run-history.json")
        self.lock = threading.Lock()
        self.gates: dict[str, dict] = {}
        self.units: dict[str, dict] = {}
        self.problem = ""
        self.pull_problem: dict[str, str] = {}
        self.pulled: dict[str, float] = {}
        self.running: set[str] = set()
        self.runner = runner or subprocess.run
        self.spawn: Callable[[Callable[[], None]], None] = lambda fn: threading.Thread(target=fn, daemon=True).start()
        self._seen: dict[str, float] = {}
        self._dirty = False
        try:
            data = json.loads(self.path.read_text())
            self.gates = {k: v for k, v in (data.get("gates") or {}).items() if isinstance(v, dict) and "started" in v}
            self.units = {k: v for k, v in (data.get("units") or {}).items() if isinstance(v, dict) and "seconds" in v}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError) as exc:
            self.problem = f"the run history could not be read ({type(exc).__name__}): starting empty"

    def gate_records(self) -> list[dict]:
        with self.lock:
            return sorted(self.gates.values(), key=lambda r: r["started"])

    def unit_records(self) -> list[dict]:
        with self.lock:
            return sorted(self.units.values(), key=lambda r: r.get("started") or 0.0)

    def add_gate(self, record: Mapping | None) -> bool:
        """Keep one gate run; a copy of the same run (its stdout and its report) merges into one record."""
        if not record:
            return False
        key = f"{record['label']}|{int(record['started'])}"
        with self.lock:
            kept = self.gates.get(key)
            if kept is not None and ("bringup" in kept["phases"] or "bringup" not in record["phases"]):
                return False
            merged = dict(record)
            if kept is not None:
                merged["path"] = kept.get("path") or record.get("path")
            self.gates[key] = merged
            if len(self.gates) > HISTORY_KEPT:
                for old in sorted(self.gates, key=lambda k: self.gates[k]["started"])[:len(self.gates) - HISTORY_KEPT]:
                    del self.gates[old]
            self._dirty = True
            return True

    def add_unit(self, name: str, started: float | None, seconds: float, ok: bool, box: str) -> None:
        with self.lock:
            if name in self.units:
                return
            self.units[name] = {"started": started, "seconds": seconds, "ok": ok, "box": box}
            self._dirty = True

    def read_local(self, roots: Sequence[Path], box: str) -> None:
        """The gate reports and fast-test logs under each root's `test-logs/`, each read once per version."""
        for root in roots:
            try:
                paths = list((root / "test-logs").glob("gates-*.log")) + list((root / "test-logs").glob("testfast-unit-*.log"))
            except OSError:
                continue
            for path in paths:
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                if self._seen.get(str(path)) == mtime:
                    continue
                self._seen[str(path)] = mtime
                if path.name.startswith("gates-"):
                    self.add_gate(gate_record(sv._read(path), box=box, path=str(path), mtime=mtime))
                    continue
                tail = sv._read(path)[-3000:]
                hit = None
                for hit in sv._UNIT_RESULT.finditer(tail):
                    pass
                if hit is not None:
                    self.add_unit(path.name, sv.gate_log_started(Path(path.name.replace("testfast-unit-", "gates-"))),
                                  float(hit.group(3)), not sv._UNIT_FAILED.search(tail), box)

    def pull(self, boxes: Sequence[str], patterns: Sequence[str], now: float) -> None:
        """Start a background pull of each box's reports when its last one is older than HISTORY_PULL_SECONDS."""
        if os.environ.get("SESSIONVIEW_WEB_REMOTE_SSH", "").strip().lower() == "off" or not patterns:
            return
        for box in boxes:
            if not re.fullmatch(r"\w+", box):
                continue
            with self.lock:
                due = box not in self.running and now - self.pulled.get(box, 0.0) >= HISTORY_PULL_SECONDS
                if due:
                    self.running.add(box)
                    self.pulled[box] = now
            if due:
                self.spawn(lambda box=box: self._fetch(box, list(patterns)))

    def _fetch(self, box: str, patterns: list[str]) -> None:
        program = (f"PATTERNS = {patterns!r}\nLIMIT = {HISTORY_FILES_PER_BOX}\nCAP = {HISTORY_READ_CAP}\n"
                   + _PULL_PROGRAM)
        try:
            done = self.runner(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", "-o", "StrictHostKeyChecking=yes",
                                box, "python3", "-"], input=program, capture_output=True, text=True,
                               timeout=HISTORY_PULL_TIMEOUT_SECONDS, check=False)
            if done.returncode != 0:
                tail = (done.stderr or "").strip().splitlines()[-1:] or ["no message"]
                raise RuntimeError(f"ssh exited {done.returncode}: {sv._redact(tail[0], 160)}")
            files = json.loads(done.stdout)
            for item in files if isinstance(files, list) else []:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    self.add_gate(gate_record(item["text"], box=box, path=str(item.get("path", "")),
                                              mtime=sv._num(item.get("mtime"))))
            problem = ""
        except subprocess.TimeoutExpired:
            problem = f"history pull from {box} did not answer in {int(HISTORY_PULL_TIMEOUT_SECONDS)}s"
        except (RuntimeError, ValueError, OSError) as exc:
            problem = f"history pull from {box} failed: {sv._redact(str(exc), 200)}"
        with self.lock:
            self.running.discard(box)
            self.pull_problem[box] = problem
        self.save()

    def save(self) -> None:
        with self.lock:
            if not self._dirty:
                return
            body = json.dumps({"gates": self.gates, "units": self.units})
            self._dirty = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(body)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
            self.problem = ""
        except OSError as exc:
            self.problem = f"could not write the run history ({type(exc).__name__}): kept in memory only"


# ---------------------------------------------------------------- the integrator's own status log

INTEGRATOR_LOG_VAR = "SESSIONVIEW_WEB_INTEGRATOR_LOG"
_LOG_TS = re.compile(r"^(?:(\d{4})-(\d{2})-(\d{2})T)?(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(Z|[+-]\d{2}:\d{2})?$")
#: A status that ends a run: the integrator exits after one batch.
RUN_END_STATES = frozenset({"done", "exit", "stop", "standdown", "done-for-now"})
#: An open run whose log has said nothing for this long may have ended without its `done` line.
RUN_QUIET_SECONDS = 2 * 3600
_BATCH_WORD = re.compile(r"\bbatch ?(\d+)", re.IGNORECASE)
_LANDED = re.compile(r"\blanded\b|\bledger \S+ pushed", re.IGNORECASE)
_NOT_LANDED = re.compile(r"\bnothing landed\b|\bno landing\b|\bNOT landed\b|\bnothing pushed\b", re.IGNORECASE)

#: Each step the log names, the words that name it, and the step that must already be named (with any
#: further ones that, when named, it must not precede): a re-run of an old batch read while cutting a new one,
#: or the tooling set's own ledger, is not this batch's step.
LOG_MARKS: tuple[tuple[str, re.Pattern, tuple[str, ...]], ...] = tuple(
    (key, re.compile(words, re.IGNORECASE), after) for key, words, after in (
        ("tooling", r"\blight set\b|\btooling (?:set|seams?) (?:landed|merged)", ()),
        ("pick", r"\bclassif|\bcut batch\b|\bbatch cut\b|\bdry-run\b|GET /integration|\bbuilding full batch\b", ()),
        ("bundle", r"\bbundle check\b|\bseam_bundle\b", ()),
        ("classify", r"\bsuite required\b|\ball-tooling\b|\bsuite-required\b", ()),
        ("merges", r"\bMERGED\b|\bmerg(?:ed|ing) \d+ seams?\b|\b\d+ seams? merged\b|\bseam_merge\b", ()),
        ("unit", r"(?<!next )\bunit lane\b|\btestfast\b", ("merges",)),
        ("unit_end", r"\bUNIT (?:GREEN|RED)\b|\bunit lane: \d+ red\b|\bunit (?:green|red)\b", ("unit",)),
        ("gate_start", r"\bGATE STARTED\b|\bgate (?:\S+ )?(?:running|launched|started)\b", ("merges",)),
        ("gate_end", r"\bfirst pass\b|\bGATE (?:DONE|GREEN|RED)\b|\bgate (?:green|red)\b", ("gate_start",)),
        ("fixup", r"\bre-?runs?\b|\brerun\w*\b|\bbatch\d+[a-z]\w*\b|\brevert|\bbounce", ("unit",)),
        ("ledger", r"\bledger\b[^|]*\bpushed\b|\bBatch\d+ landed\b|\blanded \d+ (?:rows|seams)\b", ("merges", "gate_end")),
    ))


def _zone(text: str | None) -> timezone:
    if not text or text == "Z":
        return UTC
    sign = -1 if text[0] == "-" else 1
    return timezone(sign * timedelta(hours=int(text[1:3]), minutes=int(text[4:6])))


def integrator_lines(text: str) -> list[dict]:
    """The integrator's `ts | role | status | text | next` lines with their times; a line with no readable
    time (or only a clock time before any dated line) is dropped. A clock-only time takes the date of the
    line before it, and the next day when it runs backwards."""
    out: list[dict] = []
    last: float | None = None
    for raw in (text or "").splitlines():
        parts = [p.strip() for p in raw.split(" | ")]
        if len(parts) < 4 or parts[1] != "integrator":
            continue
        hit = _LOG_TS.match(parts[0])
        if not hit:
            continue
        year, month, day, hh, mm, ss, zone = hit.groups()
        tz = _zone(zone)
        try:
            if year:
                at = datetime(int(year), int(month), int(day), int(hh), int(mm), int(ss), tzinfo=tz).timestamp()
            elif last is not None:
                at = datetime.fromtimestamp(last, tz).replace(hour=int(hh), minute=int(mm), second=int(ss)).timestamp()
                if at < last - 3600:
                    at += 86400
            else:
                continue
        except ValueError:
            continue
        last = at
        body = " | ".join(parts[3:-1]) if len(parts) >= 5 else parts[3]
        out.append({"at": at, "status": parts[2].lower(), "text": body, "next": parts[-1] if len(parts) >= 5 else ""})
    return out


def integrator_runs(lines: Sequence[Mapping]) -> list[dict]:
    """The integrator's runs, oldest first: from a line after the last run's end to its `done` (or `exit`)
    line. A second `start` inside a run is a session taking the seat over, the same run. The last run has
    `end` None while it is open."""
    runs: list[dict] = []
    cur: dict | None = None
    for line in lines:
        if cur is None:
            cur = {"start": line["at"], "end": None, "lines": []}
        cur["lines"].append(line)
        if line["status"] in RUN_END_STATES:
            cur["end"] = line["at"]
            runs.append(cur)
            cur = None
    if cur is not None:
        runs.append(cur)
    for run in runs:
        numbers = [int(n) for line in run["lines"] for n in _BATCH_WORD.findall(line["text"])]
        run["batch"] = max(numbers) if numbers else None
        last = run["lines"][-1]
        run["landed"] = bool(run["end"] is not None and _LANDED.search(last["text"])
                             and not _NOT_LANDED.search(last["text"]))
        run["marks"] = run_marks(run)
    return runs


def run_marks(run: Mapping) -> dict[str, dict]:
    """step key -> the first line of the run that names it (see LOG_MARKS); `seat` is the run's first line."""
    marks: dict[str, dict] = {"seat": run["lines"][0]} if run["lines"] else {}
    for line in run["lines"]:
        for key, words, after in LOG_MARKS:
            if key in marks or not words.search(line["text"]):
                continue
            if after and (after[0] not in marks or any(k in marks and line["at"] < marks[k]["at"] for k in after)):
                continue
            marks[key] = line
    return marks


#: Spans of the log that time a step: (history key, from mark, to mark); `end` is the run's end.
LOG_SPANS = (("pick", "seat", "bundle"), ("bundle", "bundle", "merges"), ("merges", "merges", "unit"),
             ("unit", "unit", "unit_end"), ("gate_start", "unit_end", "gate_start"), ("run", "seat", "end"))


def log_stats(runs: Sequence[Mapping], last: int = 12) -> dict[str, dict]:
    """history key -> {median, runs, ok}: each span over the ended runs that logged both of its ends."""
    out: dict[str, dict] = {}
    ended = [r for r in runs if r.get("end") is not None][-40:]
    for key, a, b in LOG_SPANS:
        pairs = []
        for run in ended:
            if key == "run" and len(run["lines"]) < 2:
                continue   # an older integrator logged only its `done` line: the run's length is unknown, not 0s
            marks = run["marks"]
            begin = marks.get(a, {}).get("at")
            finish = run["end"] if b == "end" else marks.get(b, {}).get("at")
            if begin is not None and finish is not None and finish >= begin:
                pairs.append((finish - begin, bool(run["landed"])))
        pairs = pairs[-last:]
        if key == "run":
            # A run that landed nothing (a survey, a refusal) is short and says nothing of a landing's
            # time: the median is the landed runs', the count says how many of all the runs landed.
            landed = median_stats([p for p in pairs if p[1]])
            got = dict(landed, runs=len(pairs)) if landed else None
        else:
            got = median_stats(pairs)
        if got:
            out[key] = dict(got, basis="from the integrator log")
    return out


# ---------------------------------------------------------------- the plan

#: The plan's parts, in order; the fixups come after the gate (owner, 2026-10-07).
PLAN_GROUPS = (("prepare", "Prepare the batch"), ("merge", "Merge"), ("unit", "Fast tests"), ("gate", "Gate"),
               ("fixup", "Fixups"), ("land", "Land"))

#: Every step of one integration, in order: key, part, name, what it is, optional (skipped in some runs).
PLAN_STEPS: tuple[tuple[str, str, str, str, bool], ...] = (
    ("seat", "prepare", "integrator seat taken",
     ("A fresh integrator wins the lease (lease.py challenge), declares its own pid, reads the current trunk, "
      "its role brief, the lane traps and the handoff."), False),
    ("tooling", "prepare", "tooling set landed first (light profile)",
     ("Seams that touch only tooling land first as a light set: a clean merge, no testfast, no gate "
      "(integrate_set.py). Skipped when no tooling seam is ready."), True),
    ("pick", "prepare", "batch cut",
     ("Ready seams read from GET /integration and GET /seams, each tip's Lane: trailer and merge-base checked, a "
      "seam_merge.py dry-run, ordered by priority; batch_started posted to the todo service."), False),
    ("bundle", "prepare", "pre-merge bundle check (seam_bundle.py check --semantic)",
     ("Merges the batch in a throwaway worktree and runs the standing lints, the ruff ratchet and the changed "
      "unit tests. Exit 2 names seams to drop and bounce; the check runs again on the rest."), False),
    ("classify", "prepare", "seams classified (all-tooling or suite required)",
     ("Each seam judged from its diff. Any seam touching src/ or a test of application code means the full "
      "suite runs: fast tests and the gate."), False),
    ("merges", "merge", "seams merged (seam_merge.py, --no-ff, one at a time)",
     "Each seam merged into the batch tree in priority order; the batch is pushed only with the ledger.", False),
    ("unit", "unit", sv.FAST_TESTS, dict(sv.STEP_MEANINGS).get(sv.FAST_TESTS, ""), False),
    ("gate_start", "gate", "gate launched (testgates)",
     ("The integrator checks the gate interpreter (httpx, yaml), at least 10 GiB MemAvailable and the disk, "
      "announces the gate window and starts gate_run.py from a login shell."), False),
    ("preflight", "gate", "gate preflight and plan",
     ("gate_run reclaims stale pytest temp dirs, asks the model endpoint for its models, resets stale lane "
      "stacks, declares the lanes from free memory and cores, and writes the remote boxes' slices."), False),
    ("bringup", "gate", "lanes brought up (barrier)",
     "Every lane starts its vault stack; the barrier waits until all of them are up and healthy.", False),
    ("remote", "gate", "remote slices (beside the wave)",
     "Other boxes run their share of the suites at the same time and send their evidence back.", True),
    ("wave", "gate", "wave pass (main suites, parallel lanes)", dict(sv.STEP_MEANINGS).get("wave pass", ""), False),
    ("restarts", "gate", "restart wave", dict(sv.STEP_MEANINGS).get("restart wave", "")
     + " Its suites restart containers, so they run once no lane runs a browser suite.", True),
    ("clamd", "gate", "clamd wave (the real malware engine, one lane)",
     "Gate CL dials the real clamd: one lane brings clamd up for it and puts it back afterwards.", True),
    ("alone", "gate", "alone phase", dict(sv.STEP_MEANINGS).get("alone phase", ""), True),
    ("rerun", "gate", "re-run pass (alone)",
     ("Each suite red or unmeasured in the earlier passes runs once more, alone, after one bring-up: red again "
      "is FAILED, green is 'did not reproduce'. Past --rerun-limit the pass is not run."), True),
    ("verdict", "gate", "gate verdict and report",
     ("gate_run counts every suite, applies the accepted reds, prints and writes its report and checks the "
      "benchmark floor."), False),
    ("land_check", "land", "landing preflights (seam_land.py)",
     ("Refuses before anything is written when a merge is open (MERGE_HEAD), files outside the landing are "
      "staged, or a landed row's brief has uncommitted changes."), False),
    ("briefs", "land", "landed briefs removed (git rm todo/<Id>.md)",
     "Landing a row is deleting its brief: no archive, no status mark.", False),
    ("render", "land", "MasterToDo.md Open table re-rendered and checked",
     ("gen_mastertodo.py renders the table from the remaining briefs and its --check must pass, or the tree is "
      "restored and nothing commits."), False),
    ("ledger", "land", "ledger commit (one pathspec commit)",
     "'BatchN ledger: ...' carrying the brief deletions and the table: the one ledger commit of the batch.", False),
    ("push", "land", "trunk pushed (fast-forward only)",
     "seam_land.py --push: refused, deleting nothing, when the remote trunk moved meanwhile.", False),
    ("branches", "land", "landed seam branches deleted on origin",
     ("Each landed seam/<Id> is deleted when the pushed trunk holds its tip, under a lease on that tip; a newer "
      "push stays, with a warning."), False),
    ("service", "land", "todo service told (landed facts, set finished)",
     ("landed (sha, tip) for each seam, /integration/<id>/finish and batch_finished: the rows waiting on these "
      "are freed."), False),
    ("exit", "land", "handoff written, integrator exits",
     ("The handoff is refreshed and a 'done' line logged; the overseer starts a fresh integrator for the next "
      "batch."), False),
)
_STEP = {key: (group, name, what, optional) for key, group, name, what, optional in PLAN_STEPS}
#: States that say a step has begun, for the "no record" reading of an earlier step.
_BEGUN = ("done", "RED", "RUNNING", "skipped", "left")
#: Fixups that come from a queue row: a seam fixer's row, a fixup review, a scope fix.
_FIX_ROW = re.compile(r"^(?:SEAMFIX\d*-|PREMERGE-)(.+)$|^(.+?)-(?:FIXUP|SCOPE-FIX|FIX)\b")


def _row(key: str, *, state: str = "next", start: float | None = None, end: float | None = None,
         detail: str = "", server: str = "", hist: Mapping | None = None, step: str | None = None,
         group: str | None = None, what: str | None = None, **extra: Any) -> dict:
    base = _STEP.get(key.split(":", 1)[0], (group or "fixup", key, "", False))
    return {"key": key, "group": group or base[0], "step": step or base[1], "what": base[2] if what is None else what,
            "optional": base[3], "state": state, "start": start, "end": end, "detail": detail, "server": server,
            "hist": dict(hist) if hist else None, **extra}


def _raw_kinds(raw_steps: Sequence[Mapping]) -> dict:
    """The raw steps (`build_steps`' rows) sorted by what they are."""
    kinds: dict[str, Any] = {"merge": [], "merging": None, "unit": [], "gate": None, "remote": {}, "wave": None,
                             "restarts": None, "clamd": None, "alone": None, "rerun": [], "verdict": None,
                             "sent_back": [], "landed": None}
    for st in raw_steps:
        name = str(st.get("step", ""))
        if name.startswith("merge "):
            kinds["merge"].append(st)
        elif name in ("merging now", "a git merge is open (MERGE_HEAD)"):
            kinds["merging"] = st
        elif name == sv.FAST_TESTS:
            kinds["unit"].append(st)
        elif name.startswith("gate ") and name.endswith((" started", " finished")):
            kinds["gate"] = st
        elif name.startswith("remote slice on "):
            kinds["remote"][name[len("remote slice on "):]] = st
        elif name.startswith("re-run alone: "):
            kinds["rerun"].append(st)
        elif name.startswith("gate verdict: "):
            kinds["verdict"] = st
        elif name.startswith("sent back to its owner: "):
            kinds["sent_back"].append(st)
        elif name.startswith("Batch") and name.endswith(" ledger commit"):
            kinds["landed"] = st
        else:
            for phase, start in (("wave", "wave pass"), ("restarts", "restart wave"), ("clamd", "clamd wave"),
                                 ("alone", "alone phase")):
                if name.startswith(start) and (kinds[phase] is None or st.get("state") != "next"):
                    kinds[phase] = st
    return kinds


def _raw_state(st: Mapping | None) -> str:
    state = str((st or {}).get("state", "next"))
    return {"cut off": "RED", "sent-back": "RED", "reworked": "done"}.get(state, state)


def _carry(st: Mapping | None) -> dict:
    """What a raw step brings into its plan row: its suites, its failed tests and their log."""
    return {k: st[k] for k in ("suites", "failures", "failures_total", "run") if st and k in st}


def _mentions_box(text: str, boxes: Sequence[str]) -> str:
    """The first known box named as `on <box>` in a log line ("" when none)."""
    names = {b.lower(): b for b in boxes if b}
    for word in re.findall(r"\bon (\w+)", text or "", re.IGNORECASE):
        if word.lower() in names:
            return names[word.lower()]
    for word in re.findall(r"\b(\w+):~?/", text or ""):   # a log named as `aragog:~/b92d-gate.out`
        if word.lower() in names:
            return names[word.lower()]
    return ""


def _logged_followups(run: Mapping | None, batch: int | None, known: set[str]) -> dict[str, list[dict]]:
    """label -> the integrator's lines naming it, for each later gate run of `batch` the log names (`batch92c`)
    that has no readable gate report (`known` holds the labels that do): refused, lost to a reboot, or not
    pulled yet. Read from the log's words alone."""
    if not run or batch is None:
        return {}
    named: dict[str, list[dict]] = {}
    for line in run.get("lines") or ():
        for label in re.findall(rf"\bbatch{batch}[a-z]\b", line["text"], re.IGNORECASE):
            if label.lower() not in known:
                named.setdefault(label.lower(), []).append(line)
    return named


def _record_rows(rec: Mapping, srv: str, stats: Callable[..., dict | None]) -> list[dict]:
    """The gate group's rows of one finished gate run, read from its kept record (its passes ran in order)."""
    phases, ok = rec.get("phases") or {}, rec.get("ok") or {}
    size = rec.get("scheduled")
    rows = [_row("gate_start", state="done", start=rec["started"], end=rec["started"], server=srv,
                 detail=f"{rec['label']} on {rec['box']}" + (f", {size} suites scheduled" if size else "")),
            _row("preflight", state="done", server=srv, detail="read from the run's report")]
    clock = rec["started"]
    if "bringup" in phases:
        rows.append(_row("bringup", state="done", start=clock, end=clock + phases["bringup"], server=srv,
                         hist=stats("bringup", size)))
        clock += phases["bringup"]
    else:
        rows.append(_row("bringup", state="no record", server=srv, hist=stats("bringup", size),
                         detail="the barrier's time is printed only on the run's console, which was not read"))
    for phase in sorted(p for p in phases if p.startswith("remote:")):
        box = phase.split(":", 1)[1]
        rows.append(_row(f"remote:{box}", step=f"remote slice on {box}", state="done" if ok.get(phase) else "RED",
                         start=rec["started"], end=rec["started"] + phases[phase], server=server_abbrev(box),
                         hist=stats(phase, size)))
    for phase in ("wave", "restarts", "clamd", "alone", "rerun"):
        if phase in phases:
            reds = [s for s in rec.get("reds", [])] if phase == "rerun" else []
            rows.append(_row(phase, state="done" if ok.get(phase) else "RED", start=clock, end=clock + phases[phase],
                             server=srv, hist=stats(phase, size if phase == "wave" else None),
                             detail=("still red: " + ", ".join(reds)) if reds else ""))
            clock += phases[phase]
        elif phase == "rerun" and rec.get("rerun_skipped"):
            rows.append(_row(phase, state="skipped", server=srv, hist=stats(phase),
                             detail="NOT run: more non-clean suites than --rerun-limit, so every wave verdict stands"))
        else:
            rows.append(_row(phase, state="skipped", server=srv, hist=stats(phase, size if phase == "wave" else None),
                             detail="not in this run"))
    counts = (f"{rec.get('passed')} passed, {rec.get('failed')} failed, {rec.get('unmeasured') or 0} unmeasured"
              if rec.get("passed") is not None else "")
    rows.append(_row("verdict", state="RED" if rec.get("verdict") != "GREEN" else "done", start=rec["ended"],
                     end=rec["ended"], server=srv, detail=f"GATE RUN {rec.get('verdict') or '?'}"
                     + (f": {counts}" if counts else "")))
    return rows


def _live_rows(kinds: Mapping, gate: Mapping, srv: str, stats: Callable[..., dict | None], size: int | None) -> list[dict]:
    """The gate group's rows of the gate this page reads live (or read from its report), from the raw steps."""
    running = gate.get("state") == "running"
    over = kinds["verdict"] is not None or gate.get("state") == "finished"
    started = sv._num(gate.get("started_at"))
    rows = [_row("gate_start", state="done", start=started, end=started, server=srv,
                 detail=f"{gate.get('label', '?')} on {gate.get('box') or 'this box'}" + (f", {size} suites" if size else ""))]
    passes_begun = any(kinds[p] is not None and _raw_state(kinds[p]) != "next" for p in ("wave", "restarts", "clamd", "alone"))
    rows.append(_row("preflight", state="done" if passes_begun or over else "RUNNING" if running else "next",
                     start=started, server=srv))
    wave = kinds["wave"]
    wave_live = wave is not None and _raw_state(wave) == "RUNNING"
    suites_moving = wave is not None and any(r.get("state") in ("RUNNING", "passed", "RED") for r in wave.get("suites", []))
    rows.append(_row("bringup", state="done" if (passes_begun and (suites_moving or not wave_live)) or over
                     else "RUNNING" if wave_live else "next", server=srv, hist=stats("bringup", size),
                     detail="" if suites_moving or over else "the lanes start their stacks behind the barrier"))
    for box, st in sorted(kinds["remote"].items()):
        rows.append(_row(f"remote:{box}", step=f"remote slice on {box}", state=_raw_state(st),
                         start=st.get("start_ts"), end=st.get("end_ts"), server=server_abbrev(box),
                         detail=str(st.get("detail", "")), hist=stats(f"remote:{box}", size), **_carry(st)))
    for phase in ("wave", "restarts", "clamd", "alone"):
        st = kinds[phase]
        if st is None:
            rows.append(_row(phase, state="skipped" if over else "next", server=srv,
                             hist=stats(phase, size if phase == "wave" else None),
                             detail="not in this run" if over else ""))
            continue
        rows.append(_row(phase, state=_raw_state(st), start=st.get("start_ts"), end=st.get("end_ts"), server=srv,
                         detail=str(st.get("detail", "")), hist=stats(phase, size if phase == "wave" else None),
                         **_carry(st)))
    reruns = kinds["rerun"]
    if reruns:
        states = [_raw_state(r) for r in reruns]
        state = "RUNNING" if "RUNNING" in states else "next" if all(s == "next" for s in states) else \
            "RED" if "RED" in states else "done"
        starts = [r.get("start_ts") for r in reruns if r.get("start_ts")]
        ends = [r.get("end_ts") for r in reruns if r.get("end_ts")]
        rows.append(_row("rerun", state=state, start=min(starts) if starts else None,
                         end=max(ends) if ends and state not in ("RUNNING", "next") else None, server=srv,
                         hist=stats("rerun"), items=[{"name": str(r["step"]).split(": ", 1)[-1], "state": _raw_state(r),
                                                      "text": str(r.get("detail", ""))} for r in reruns],
                         detail=f"{len(reruns)} suite(s): " + ", ".join(str(r["step"]).split(": ", 1)[-1] for r in reruns)))
    else:
        rows.append(_row("rerun", state="skipped" if over else "next", server=srv, hist=stats("rerun"),
                         detail="nothing to re-run" if over else ""))
    verdict = kinds["verdict"]
    rows.append(_row("verdict", state=_raw_state(verdict) if verdict else "next", server=srv,
                     start=verdict.get("end_ts") if verdict else None, end=verdict.get("end_ts") if verdict else None,
                     detail=str(verdict["step"]).split(": ", 1)[-1] + (" " + str(verdict.get("detail")) if verdict.get("detail") else "")
                     if verdict else ""))
    return rows


TEARDOWN_STEP = "teardown (make room for the alone phase)"
TEARDOWN_WHAT = ("Lane stacks brought down with their volumes (gate_lane.sh with LANE_TEARDOWN), by the integrator "
                 "(label teardown<N>) or by gate_run before its re-run pass (<label>-rerun), so a suite that needs "
                 "the box alone can run. A step of its batch, repeated as often as it is needed.")


def teardown_rows(teardowns: Sequence[Mapping], batch: int | None, since: float | None) -> list[dict]:
    """One row per teardown of batch `batch` (a label on a box), each lane an item with its server and time.

    The reason is what the lane logs say, or nothing when they say nothing.
    """
    events: dict[tuple[str, str], list[Mapping]] = {}
    for item in teardowns:
        if not isinstance(item, Mapping) or not isinstance(item.get("label"), str) or sv._num(item.get("end")) is None:
            continue
        if batch is None or sv.teardown_batch(item["label"]) != batch:
            continue
        if since is not None and item["end"] < since - 3600:
            continue
        events.setdefault((str(item.get("box", "")), item["label"]), []).append(item)
    # A teardown's usual time: one lane's, over every lane torn down that the logs read here show.
    per_lane = median_stats([(x["end"] - sv._num(x.get("start")), bool(x.get("ok", True))) for x in teardowns
                             if isinstance(x, Mapping) and sv._num(x.get("end")) is not None
                             and sv._num(x.get("start")) is not None and x["end"] >= sv._num(x.get("start"))])
    hist = dict(per_lane, basis="one lane, the teardowns read here") if per_lane else None
    rows = []
    for (box, label), lanes in sorted(events.items(), key=lambda kv: min(x["end"] for x in kv[1])):
        lanes = sorted(lanes, key=lambda x: (x["end"], x.get("lane", 0)))
        starts = [sv._num(x.get("start")) for x in lanes if sv._num(x.get("start"))]
        reasons = sorted({str(x.get("reason") or "") for x in lanes} - {""})
        srv = server_abbrev(box)
        items = []
        for x in lanes:
            begun = sv._num(x.get("start"))
            took = sv.fmt_seconds(x["end"] - begun) if begun and x["end"] >= begun else ""
            items.append({"name": f"{srv} lane {x.get('lane', '?')}", "state": "done" if x.get("ok", True) else "RED",
                          "text": ", ".join(t for t in (f"took {took}" if took else "", "ended " + sv._stamp(x["end"]),
                                                         str(x.get("reason") or "")) if t)})
        rows.append(_row(f"teardown:{box}:{label}", group="gate", step=TEARDOWN_STEP, what=TEARDOWN_WHAT,
                         state="done" if all(x.get("ok", True) for x in lanes) else "RED",
                         start=min(starts) if starts else None, end=max(x["end"] for x in lanes), server=srv, hist=hist,
                         detail=f"{label}: {len(lanes)} lane(s) " + ", ".join(str(x.get("lane", "?")) for x in lanes)
                         + (f"; reason: {' / '.join(reasons)}" if reasons else ""), items=items))
    return rows


def build_plan(ev: Mapping) -> dict:
    """The plan: every step of the run in order, each with state, server, times and history; pure.

    `ev` keys (every one optional): now, here (this box), boxes (known box names), raw (build_steps' rows),
    gate (the top gate), gate_box, run (the integrator log's open run), last_run (its last ended run),
    log_stats, records (kept gate runs), units (kept fast-test runs), seams (this batch's seam ids),
    seam_rows (GET /seams rows), queue (GET /queue rows: open, claimed, blocked), teardowns (lane teardown
    runs from the lane logs, each with its box).
    """
    now = float(ev.get("now") or time.time())
    here = server_abbrev(ev.get("here", ""))
    boxes = list(ev.get("boxes") or [])
    raw = list(ev.get("raw") or [])
    kinds = _raw_kinds(raw)
    run = ev.get("run")
    marks = (run or {}).get("marks") or {}
    lstats = dict(ev.get("log_stats") or {})
    records = list(ev.get("records") or [])
    gate = dict(ev.get("gate") or {})
    batch_no = (run or {}).get("batch")
    if run is not None:
        # What ended before this run began is the last run's (its merges, its ledger, its sent-back seams).
        def fresh(st: Mapping) -> bool:
            return (sv._num(st.get("end_ts")) or sv._num(st.get("start_ts")) or now) >= run["start"] - 60
        kinds["merge"] = [m for m in kinds["merge"] if fresh(m)]
        kinds["sent_back"] = [st for st in kinds["sent_back"] if fresh(st)]
        if kinds["landed"] is not None and not fresh(kinds["landed"]):
            kinds["landed"] = None
    last_end = (ev.get("last_run") or {}).get("end")
    # A merge that ended before the last run did is that run's (its tooling set, say), not a new batch's.
    merged_since = [m for m in kinds["merge"] if last_end is None or (sv._num(m.get("end_ts")) or 0.0) > last_end]
    if run is None and ev.get("last_run") and not (merged_since or kinds["merging"] or gate.get("state") == "running"):
        # Between batches by the integrator's own log: what the page reads of the last batch is history,
        # and the plan is the next run's, every step still to come.
        kinds, gate = _raw_kinds([]), {}

    def stats(phase: str, size: int | None = None) -> dict | None:
        return phase_stats(records, phase, size=size)

    # The integrator's own fast-test runs first (from its log); the fast-test logs this box keeps are any
    # session's runs, the batch's among them, and are the fallback.
    unit_hist = median_stats([(u["seconds"], bool(u.get("ok"))) for u in list(ev.get("units") or [])[-12:]])
    unit_hist = lstats["unit"] if (lstats.get("unit") or {}).get("runs", 0) >= 2 else \
        dict(unit_hist, basis="fast-test logs here") if unit_hist else lstats.get("unit")
    rows: list[dict] = []

    def mark(key: str) -> dict | None:
        return marks.get(key)

    # ---- prepare
    seat = mark("seat")
    rows.append(_row("seat", state="done" if seat else "next", start=seat and seat["at"], end=seat and seat["at"],
                     server=here, detail=(seat or {}).get("text", "")[:160]))
    tooling = mark("tooling")
    rows.append(_row("tooling", state="done" if tooling else "next", start=tooling and tooling["at"],
                     end=tooling and tooling["at"], server=here, detail=(tooling or {}).get("text", "")[:160]))
    later_marks = ("bundle", "classify", "merges", "unit", "gate_start")
    for key in ("pick", "bundle", "classify"):
        hit = mark(key)
        ends = [m["at"] for k in later_marks[later_marks.index(key) + 1 if key in later_marks else 0:]
                if (m := mark(k)) and hit and m["at"] >= hit["at"]]
        end = min(ends) if ends and key != "classify" else None    # a classification is one moment
        state = "next" if not hit else "done" if end or key == "classify" else "RUNNING"
        rows.append(_row(key, state=state, start=hit and hit["at"], end=end if hit else None, server=here,
                         hist=lstats.get(key), detail=(hit or {}).get("text", "")[:160]))

    # ---- merge
    merges = kinds["merge"]
    merging = kinds["merging"]
    if merges or merging:
        starts = [m.get("start_ts") or m.get("end_ts") for m in merges if m.get("start_ts") or m.get("end_ts")]
        ends = [m.get("end_ts") for m in merges if m.get("end_ts")]
        names = [str(m["step"])[len("merge "):] for m in merges]
        rows.append(_row("merges", state="RUNNING" if merging else "done",
                         start=min(starts) if starts else (merging or {}).get("start_ts"),
                         end=None if merging else (max(ends) if ends else None), server=here,
                         hist=lstats.get("merges"),
                         detail=(f"{len(names)} merged: " + ", ".join(names) if names else "")
                         + (f"; now: {merging.get('detail')}" if merging else ""),
                         items=[{"name": n, "state": "done", "text": str(m.get("detail", ""))} for n, m in zip(names, merges)]))
    elif mark("merges"):
        hit = mark("merges")
        rows.append(_row("merges", state="done", start=hit["at"], end=hit["at"], server=here, hist=lstats.get("merges"),
                         detail=hit["text"][:200]))
    else:
        rows.append(_row("merges", server=here, hist=lstats.get("merges")))

    # ---- fast tests: the newest run since the batch began
    begin = (run or {}).get("start")
    units = [u for u in kinds["unit"] if begin is None or (u.get("end_ts") or now) >= begin]
    unit_box = _mentions_box((mark("unit") or {}).get("text", "") + " " + (mark("unit_end") or {}).get("text", ""), boxes)
    if units:
        st = units[-1]
        rows.append(_row("unit", state=_raw_state(st), start=st.get("start_ts"), end=st.get("end_ts"), server=here,
                         detail=str(st.get("detail", "")) + (f" ({len(units)} runs this batch)" if len(units) > 1 else ""),
                         hist=unit_hist, **_carry(st)))
    elif mark("unit"):
        done_line = mark("unit_end")
        red = bool(done_line and re.search(r"\bred\b", done_line["text"], re.IGNORECASE)
                   and not re.search(r"\bgreen\b", done_line["text"], re.IGNORECASE))
        rows.append(_row("unit", state=("RED" if red else "done") if done_line else "RUNNING",
                         start=mark("unit")["at"], end=done_line["at"] if done_line else None,
                         server=server_abbrev(unit_box) or here, hist=unit_hist,
                         detail=((done_line or mark("unit"))["text"])[:200] + " (from the integrator log)"))
    else:
        rows.append(_row("unit", server=server_abbrev(unit_box) or here, hist=unit_hist))

    # ---- gate: this batch's first gate run; later runs of the batch are fixups
    top_label = str(gate.get("label", ""))
    top_box = str(ev.get("gate_box") or gate.get("box") or ev.get("here", ""))
    own = [r for r in records if batch_no is not None and r.get("batch") == batch_no
           and (begin is None or r["started"] >= begin - 3600)]
    top_is_batch = bool(top_label) and (batch_no is None or label_batch(top_label) == batch_no) \
        and (begin is None or (sv._num(gate.get("started_at")) or now) >= begin - 3600)
    labels = [r["label"] for r in own]
    main_label = own[0]["label"] if own else (top_label if top_is_batch else "")
    if top_is_batch and own and (sv._num(gate.get("started_at")) or now) < own[0]["started"]:
        main_label = top_label
    size = len(gate.get("rows") or []) or None
    gate_rows: list[dict]
    if top_is_batch and top_label == main_label:
        gate_rows = _live_rows(kinds, gate, server_abbrev(top_box), stats, size)
    elif own and own[0]["label"] == main_label:
        gate_rows = _record_rows(own[0], server_abbrev(own[0]["box"]), stats)
    else:
        started_line = mark("gate_start")
        gate_srv = server_abbrev(_mentions_box((started_line or {}).get("text", ""), boxes))
        gate_rows = [_row("gate_start", state="done" if started_line else "next", server=gate_srv,
                          start=started_line and started_line["at"], end=started_line and started_line["at"],
                          hist=lstats.get("gate_start"),
                          detail=(started_line or {}).get("text", "")[:200])]
        end_line = mark("gate_end")
        for key in ("preflight", "bringup", "wave", "restarts", "clamd", "alone", "rerun"):
            # Logged as over: each pass happened, its time is in the gate's report, not read here yet.
            gate_rows.append(_row(key, state="no record" if end_line or (started_line and key == "preflight") else "next",
                                  server=gate_srv, hist=stats(key, None)))
        red_end = bool(end_line and re.search(r"\bRED\b|\bred\b", end_line["text"]))
        gate_rows.append(_row("verdict", state=("RED" if red_end else "done") if end_line else "next", server=gate_srv,
                              start=end_line and end_line["at"], end=end_line and end_line["at"],
                              detail=(end_line or {}).get("text", "")[:200]))
        if started_line:
            gate_rows[0]["detail"] = (gate_rows[0]["detail"] + " (from the integrator log; its gate report is not "
                                      "read here yet)").strip()
    if not any(r["key"] == "gate_start" and r["hist"] for r in gate_rows):
        gate_rows[0]["hist"] = lstats.get("gate_start")
    # Lanes torn down for this batch: in the gate before its alone and re-run passes while the first run
    # lasts, a fixup step after it.
    main_end = (own[0]["ended"] if own and own[0]["label"] == main_label else
                sv._num(gate.get("ended_at")) if top_label == main_label and gate.get("state") == "finished" else None)
    tear = teardown_rows(ev.get("teardowns") or [], batch_no if batch_no is not None else label_batch(main_label),
                         begin)
    late_tear = [t for t in tear if main_end is not None and t["end"] > main_end]
    early_tear = [t for t in tear if t not in late_tear]
    at = next((i for i, r in enumerate(gate_rows) if r["key"] in ("alone", "rerun")), len(gate_rows))
    gate_rows[at:at] = early_tear
    rows += gate_rows

    # ---- fixups
    fixups: list[dict] = []
    for row in late_tear:
        row["group"] = "fixup"
    follow = [r for r in own if r["label"] != main_label]
    for rec in follow:
        fixups.append(_row(f"fixup:{rec['label']}", group="fixup",
                           step=f"gate re-run {rec['label']}" + (f" ({rec['scheduled']} suites)" if rec.get("scheduled") else ""),
                           what="A later gate run of this batch: reds re-run, or the rest of a run that broke.",
                           state="done" if rec.get("verdict") == "GREEN" else "RED", start=rec["started"], end=rec["ended"],
                           server=server_abbrev(rec["box"]), hist=run_stats(records, kind="followup", size=rec.get("scheduled")),
                           detail=f"GATE RUN {rec.get('verdict') or '?'}: {rec.get('passed')} passed, {rec.get('failed')} failed, "
                                  f"{rec.get('unmeasured') or 0} unmeasured" + (f"; still red: {', '.join(rec['reds'])}" if rec.get("reds") else "")))
    top_follow = top_is_batch and top_label != main_label and top_label not in labels
    if top_follow:
        wave = kinds["wave"]
        running = gate.get("state") == "running"
        fixups.append(_row(f"fixup:{top_label}", group="fixup", step=f"gate re-run {top_label}" + (f" ({size} suites)" if size else ""),
                           what="A later gate run of this batch: reds re-run, or the rest of a run that broke.",
                           state="RUNNING" if running else _raw_state(kinds["verdict"]) if kinds["verdict"] else "done",
                           start=sv._num(gate.get("started_at")), end=None if running else sv._num(gate.get("ended_at")),
                           server=server_abbrev(top_box), hist=run_stats(records, kind="followup", size=size),
                           detail=str((wave or {}).get("detail", "")), **_carry(wave)))
    fixups += late_tear
    known = {str(x).lower() for x in labels} | ({str(top_label).lower()} if top_is_batch and top_label else set())
    unread = _logged_followups(run, batch_no, known)
    for label, lines in unread.items():
        fixups.append(_row(f"fixup:{label}", group="fixup", step=f"gate re-run {label}",
                           what="A later gate run of this batch that the integrator's log names but whose gate "
                                "report is not read here: refused before it ran, lost to a reboot, or not pulled yet.",
                           state="no record", start=lines[0]["at"], end=lines[-1]["at"],
                           server=server_abbrev(next((b for b in (_mentions_box(ln["text"], boxes) for ln in lines) if b), "")),
                           detail="no gate report; the log says: " + lines[-1]["text"][:200]))
    if not follow and not top_follow and not unread and mark("fixup") and run:
        # No later gate run of this batch is readable here yet: what the integrator logged of its re-runs and
        # fixes is the record, one line each.
        words = next(w for k, w, _ in LOG_MARKS if k == "fixup")
        lines = [ln for ln in run["lines"] if ln["at"] >= mark("fixup")["at"] and words.search(ln["text"])]
        still = run.get("end") is None and not kinds["landed"]
        fixups.append(_row("fixup:log", group="fixup", step=f"re-runs and fixes the integrator logged ({len(lines)})",
                           what="Lines of the integrator's log after its first gate pass that name a re-run, a revert "
                                "or a bounce; the gate reports of those runs are not read here yet.",
                           state="RUNNING" if still else "done", start=lines[0]["at"],
                           end=None if still else lines[-1]["at"], server=here,
                           hist=run_stats(records, kind="followup"), detail=lines[-1]["text"][:200],
                           items=[{"name": sv._stamp(ln["at"], now), "state": "done", "text": ln["text"][:200]}
                                  for ln in lines[-20:]]))
    fixups.sort(key=lambda r: r["start"] or r["end"] or now)   # the later gate runs and teardowns, as they happened
    # The reds still open after the newest finished run of this batch, each a fixup to come.
    latest = None
    if not (top_is_batch and gate.get("state") == "running"):
        finished = sorted(own, key=lambda r: r["started"])
        latest = finished[-1] if finished else None
    if top_follow:
        # The newest run of this batch is the one on the board, not kept yet: its verdict says what is still red.
        latest = None
        verdict = kinds["verdict"]
        if verdict and _raw_state(verdict) == "RED" and gate.get("state") != "running" and not kinds["landed"]:
            fixups.append(_row("reds", group="fixup", step="reds to clear",
                               what="Reds after the newest gate run of this batch: re-run alone in one later gate "
                                    "run, fix the seam that broke one (revert and bounce), or name it a known red.",
                               state="next", server=server_abbrev(top_box), hist=run_stats(records, kind="followup"),
                               detail=f"red in {top_label}: " + str(verdict.get("detail") or verdict["step"])[:200]))
    if latest and latest.get("verdict") != "GREEN" and not kinds["landed"] and latest.get("reds"):
        # One later gate run clears them together, so its time is a follow-up run's of that many suites.
        reds = list(latest["reds"])
        fixups.append(_row("reds", group="fixup", step=f"reds to clear ({len(reds)})",
                           what="Reds after the newest gate run of this batch: re-run alone in one later gate run, "
                                "fix the seam that broke one (revert and bounce), or name it a known red.",
                           state="next", server=server_abbrev(latest["box"]),
                           hist=run_stats(records, kind="followup", size=len(reds)),
                           detail=f"red in {latest['label']}: " + ", ".join(reds[:30]),
                           items=[{"name": suite, "state": "RED", "text": f"red in {latest['label']}"}
                                  for suite in reds[:30]]))
    for st in kinds["sent_back"]:
        fixups.append(_row("sent:" + str(st["step"]).split(": ", 1)[-1], group="fixup", step=str(st["step"]),
                           what="A seam the integrator reverted and sent back to its owner; the batch lands without it.",
                           state=_raw_state(st), end=st.get("end_ts"), server=here, detail=str(st.get("detail", ""))))
    unit_row = next(r for r in rows if r["key"] == "unit")
    if unit_row["state"] == "RED" and not kinds["landed"]:
        fixups.append(_row("unit-red", group="fixup", step="fast tests red: classify, revert or fix, run again",
                           what="lane-red-triage first: a flake, the lane's environment, a red already on the trunk, or a "
                                "seam's regression (revert it and bounce it with seam_bounce.py).",
                           state="next", server=unit_row["server"], hist=unit_hist, detail=unit_row["detail"]))
    batch_ids = {str(s) for s in ev.get("seams") or ()}
    if batch_ids:
        for bucket, state in (("claimed", "RUNNING"), ("open", "next"), ("blocked", "blocked")):
            for item in (ev.get("queue") or {}).get(bucket) or []:
                rid = str((item or {}).get("id", ""))
                hit = _FIX_ROW.match(rid)
                target = (hit.group(1) or hit.group(2)) if hit else ""
                if target in batch_ids:
                    fixups.append(_row(f"queue:{rid}", group="fixup", step=f"queue row {rid}",
                                       what="A fix row on the todo service for a seam of this batch.",
                                       state=state, server=server_abbrev(item.get("box", "")),
                                       detail=str(item.get("reason") or item.get("title") or "")[:200]))
    if not fixups:
        fixups.append(_row("fixup:none", group="fixup", step="none needed so far", what="", state="none",
                           detail="no red to clear, no seam sent back, no fix row for this batch's seams"))
    rows += fixups

    # ---- land
    landed = kinds["landed"]
    ledger_line = mark("ledger")
    land_at = (landed or {}).get("end_ts") or (ledger_line or {}).get("at")
    for key in ("land_check", "briefs", "render", "ledger", "push"):
        rows.append(_row(key, state="done" if land_at else "next", start=land_at, end=land_at, server=here,
                         detail=(str((landed or {}).get("detail", "")) or (ledger_line or {}).get("text", ""))[:200]
                         if key == "ledger" and land_at else ""))
    left = []
    if land_at:
        listed = {str(r.get("id", "")) for r in ev.get("seam_rows") or ()}
        left = sorted(batch_ids & listed)
    rows.append(_row("branches", state=("left" if left else "done") if land_at else "next", start=land_at,
                     end=land_at if land_at and not left else None, server=here,
                     detail=f"still on origin: {', '.join(left)}" if left else ""))
    ended = bool(run and run.get("end")) or (bool(land_at) and ev.get("last_run") and
                                             (ev["last_run"].get("end") or 0) >= land_at)
    rows.append(_row("service", state="done" if land_at and ended else "next", server=here))
    exit_at = (run or {}).get("end") if run else ((ev.get("last_run") or {}).get("end") if land_at else None)
    rows.append(_row("exit", state="done" if exit_at and land_at else "next", start=exit_at, end=exit_at, server=here,
                     hist=lstats.get("land")))
    return _finish(rows, ev, now, lstats)


def _finish(rows: list[dict], ev: Mapping, now: float, lstats: Mapping) -> dict:
    """Mark the unrecorded steps, number the rows, time them, and sum up each part and the run."""
    order = [r for r in rows if r["group"] != "fixup"]
    for i, row in enumerate(order):
        if row["group"] == "prepare" and row["state"] == "RUNNING" and any(
                later["state"] in _BEGUN for later in order[i + 1:]):
            row["state"] = "done"           # its end was not logged, and the run has moved on
        if row["state"] == "next" and any(later["state"] in _BEGUN for later in order[i + 1:]):
            row["state"] = "skipped" if row["optional"] else "no record"
            if not row["detail"]:
                row["detail"] = ("not in this run, or not logged" if row["optional"]
                                 else "not recorded where this page reads; a later step has begun")
    out_rows = []
    remaining, unknown = 0.0, 0
    for n, row in enumerate(rows, 1):
        hist = row.pop("hist")
        start, end = row.pop("start"), row.pop("end")
        out = {**row, "n": n, "started": sv._stamp(start, now), "finished": sv._stamp(end, now),
               "history": history_text(hist), "median_s": hist["median"] if hist else None}
        out.pop("optional", None)
        if row["state"] == "RUNNING" and start:
            out["elapsed_s"] = max(0.0, now - start)
            out["took"] = sv.fmt_seconds(now - start) + " so far"
            out["eta"] = eta_text(out["median_s"], out["elapsed_s"])
        elif start and end and end >= start and end - start >= 1:
            out["took"] = sv.fmt_seconds(end - start)
        if row["state"] in ("next", "RUNNING"):
            if row["state"] == "next":
                out["eta"] = eta_text(out["median_s"], 0.0)
            if out["median_s"] is None:
                unknown += 1 if row["key"] != "fixup:none" else 0
            else:
                remaining += max(0.0, out["median_s"] - out.get("elapsed_s", 0.0))
        out_rows.append(out)
    groups = []
    for key, title in PLAN_GROUPS:
        mine = [r for r in out_rows if r["group"] == key]
        states = {r["state"] for r in mine}
        state = ("none" if states == {"none"} else
                 "RUNNING" if "RUNNING" in states else "next" if states <= {"next", "none"} and "next" in states
                 else "RED" if "RED" in states and "next" not in states else
                 "partly" if "next" in states else "done")
        groups.append({"key": key, "title": title, "state": state, "rows": mine,
                       "summary": _group_summary(mine, state)})
    run = ev.get("run")
    head = {}
    if run:
        head = {"state": "running", "batch": f"Batch{run['batch']}" if run.get("batch") is not None else "",
                "started": sv._stamp(run["start"], now), "running_for": sv.fmt_seconds(now - run["start"]),
                "last_line": run["lines"][-1]["text"][:240], "last_next": run["lines"][-1]["next"][:160],
                "last_at": sv._stamp(run["lines"][-1]["at"], now)}
        if now - run["lines"][-1]["at"] > RUN_QUIET_SECONDS:
            head["quiet"] = (f"the integrator log has said nothing for {sv.fmt_seconds(now - run['lines'][-1]['at'])}: "
                             "the run may have ended without its done line")
    elif ev.get("last_run"):
        last = ev["last_run"]
        head = {"state": "between", "last_end": sv._stamp(last["end"], now),
                "last_batch": f"Batch{last['batch']}" if last.get("batch") is not None else "",
                "last_line": last["lines"][-1]["text"][:240], "landed": bool(last.get("landed"))}
    whole = lstats.get("run")
    return {"head": head, "groups": groups, "remaining_s": remaining, "remaining_unknown": unknown,
            "remaining": sv.fmt_seconds(remaining) if remaining else "",
            "whole_run": (f"{sv.fmt_seconds(whole['median'])} median of the runs that landed; {whole['ok']} of the last "
                          f"{whole['runs']} runs landed (from the integrator log)") if whole else "",
            "generated": now}


def _group_summary(rows: Sequence[Mapping], state: str) -> str:
    if state == "none":
        return "none needed so far"
    if state == "next":
        known = sum(r["median_s"] for r in rows if r.get("median_s") is not None)
        return f"next: about {sv.fmt_seconds(known)}" if known >= 1 else "next"
    running = [r for r in rows if r["state"] == "RUNNING"]
    if running:
        lead = running[0]
        return "now: " + lead["step"] + (" " + lead["eta"] if lead.get("eta") else "")
    reds = [r for r in rows if r["state"] in ("RED", "left")]
    done = [r for r in rows if r["state"] == "done"]
    return ", ".join(x for x in (f"{len(done)} done" if done else "", f"{len(reds)} red" if reds else "") if x) or state


# ---------------------------------------------------------------- the section's gatherer

def integration_plan(run_section: Mapping, sections: Mapping, history: RunHistory | None, log_text: str | None,
                     now: float, *, here: str = "", boxes: Sequence[str] = ()) -> dict:
    """The plan for the Integrator now box, from the box's own section, the todo service's sections, the kept
    history and the integrator log's text (None when the log is not configured or not readable)."""
    runs = integrator_runs(integrator_lines(log_text or ""))
    open_run = runs[-1] if runs and runs[-1]["end"] is None else None
    last_run = next((r for r in reversed(runs) if r["end"] is not None), None)
    batch_now = sections.get("integrator_run", {}).get("batch_now") if isinstance(sections.get("integrator_run"), Mapping) else None
    seams = {str(s.get("seam")) for s in run_section.get("seams_in_batch") or () if isinstance(s, Mapping)}
    seams |= {str(s.get("seam")) for s in (batch_now or {}).get("seams") or () if isinstance(s, Mapping)
              and (batch_now or {}).get("state") in ("merged", "running")}
    seamstatus = sections.get("seamstatus") if isinstance(sections.get("seamstatus"), Mapping) else {}
    queue = sections.get("queue") if isinstance(sections.get("queue"), Mapping) else {}
    ev = {"now": now, "here": here, "boxes": list(boxes), "raw": run_section.get("steps") or [],
          "gate": run_section.get("gate") if isinstance(run_section.get("gate"), Mapping) else {},
          "gate_box": run_section.get("gate_box", ""), "run": open_run, "last_run": last_run,
          "log_stats": log_stats(runs), "records": history.gate_records() if history else [],
          "units": history.unit_records() if history else [], "seams": sorted(s for s in seams if s and s != "None"),
          "seam_rows": seamstatus.get("seams") if seamstatus.get("state") == "ok" else [],
          "queue": queue if queue.get("state") == "ok" else {},
          "teardowns": [t for t in run_section.get("teardowns") or () if isinstance(t, Mapping)]}
    plan = build_plan(ev)
    notes = []
    if log_text is None:
        notes.append(f"{INTEGRATOR_LOG_VAR} unset or unreadable: the steps before the merges are read from nothing here")
    if history is not None:
        if history.problem:
            notes.append(history.problem)
        notes += [p for p in history.pull_problem.values() if p]
    plan["notes"] = notes
    return plan


def read_integrator_log() -> str | None:
    """The integrator log's text (its last 400 KB), or None when `SESSIONVIEW_WEB_INTEGRATOR_LOG` is unset or unreadable."""
    named = os.environ.get(INTEGRATOR_LOG_VAR, "").strip()
    if not named:
        return None
    path = Path(named).expanduser()
    try:
        with path.open("rb") as handle:
            size = path.stat().st_size
            handle.seek(max(0, size - 400_000))
            return handle.read().decode("utf-8", "replace")
    except OSError:
        return None


def current_batch(log_text: str | None) -> int | None:
    """The batch number the integrator's open run names, or None when no run is open."""
    runs = integrator_runs(integrator_lines(log_text or ""))
    return runs[-1]["batch"] if runs and runs[-1]["end"] is None else None
