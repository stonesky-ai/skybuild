"""Gate logs: where a batch's `gates-*.log` is, how it is read, and the scratch roots a setting names.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

from . import hub as sv

# ---------------------------------------------------------------- integration steps that stay

_GATE_HEAD = re.compile(r"^lane (\w+)(?: \(remote box\))?\s+wall (\d+)s\s+exit=(-?\d+)\s+log=(\S+)(.*)$")
_GATE_SUITE = re.compile(r"^\s{2}(passed|red|FAILED|did not reproduce|UNMEASURED)\s+(\S+)\s+(.*)$")
_GATE_STAMP = re.compile(r"gates-(\d{8}T\d{6})Z")
_PLAN_REMOTE = re.compile(r"^(\d+) remote slice\(s\) of candidate (\w+): (.*)$")
_PLAN_BOX = re.compile(r"(\w+) \((\d+) suite")
_PLAN_RERUN = re.compile(r"^re-run pass on lane (\d+), alone, once each, (.+?): (\d+) non-clean suite\(s\): (.*)$")
#: The verdict line; gate_run may follow the word with why (`GATE RUN GREEN (1 did not reproduce, ...)`).
_VERDICT = re.compile(r"^(GATE RUN \w+)(?: \(.*\))?$")
#: A step observed live is kept this long after it vanishes.
OBSERVED_CAP = 300
#: An idle stretch between one step ending and the next beginning is shown from this long.
GAP_SECONDS = 300


def _stamp(value: float | None, now: float | None = None) -> str:
    if not value:
        return ""
    today = sv.zoned(now if now is not None else time.time(), "%m-%d")[:5]
    fmt = "%H:%M:%S" if sv.zoned(value, "%m-%d")[:5] == today else "%m-%d %H:%M:%S"
    return sv.zoned(value, fmt)


def parse_gate_log(text: str) -> dict:
    """gate_run's own console log: its plan up front, then one block per lane pass, then the verdict."""
    out: dict[str, Any] = {"label": "", "plan": [], "blocks": [], "verdict": [], "boxes": {}, "reruns": []}
    block = None
    for line in text.splitlines():
        if line.startswith("gate run ") and not out["label"]:
            out["label"] = line[len("gate run "):].strip()
            continue
        head = _GATE_HEAD.match(line)
        if head:
            block = {"lane": head.group(1), "wall": int(head.group(2)), "exit": int(head.group(3)),
                     "log": head.group(4), "note": head.group(5).strip(), "suites": [],
                     "remote": "(remote box)" in line}
            out["blocks"].append(block)
            continue
        hit = _GATE_SUITE.match(line)
        if hit and block is not None:
            block["suites"].append({"state": hit.group(1), "suite": hit.group(2), "text": hit.group(3)[:200]})
            continue
        if not line.strip():
            block = None if block is not None and block["suites"] else block
            continue
        if block is None and not out["blocks"]:
            out["plan"].append(line)
            remote = _PLAN_REMOTE.match(line)
            if remote:
                out["boxes"] = {b: int(n) for b, n in _PLAN_BOX.findall(remote.group(3))}
            rerun = _PLAN_RERUN.match(line)
            if rerun:
                out["reruns"].append({"when": rerun.group(2), "suites": [x.strip() for x in rerun.group(4).split(",")]})
            continue
        if _VERDICT.match(line) or line.startswith(("FAILED:", "DID NOT REPRODUCE:", "BENCHMARK", "batch split")) \
                or re.match(r"^\d+ passed, \d+ failed", line):
            out["verdict"].append(line[:300])
    return out


def gate_log_started(path: Path) -> float | None:
    hit = _GATE_STAMP.search(path.name)
    if not hit:
        return None
    import calendar
    return float(calendar.timegm(time.strptime(hit.group(1), "%Y%m%dT%H%M%S")))


def batch_timeline_logs(roots: list[Path]) -> dict[str, list[dict]]:
    """Read persisted gate and unit pass boundaries from the test log roots."""
    gates: list[dict] = []
    units: list[dict] = []
    for root in roots:
        try:
            gate_paths = sorted((root / "test-logs").glob("gates-*.log"))
            unit_paths = sorted((root / "test-logs").glob("testfast-unit-*.log"))
        except OSError:
            continue
        for path in gate_paths:
            started = gate_log_started(path)
            if started is None:
                continue
            try:
                ended = path.stat().st_mtime
                parsed = parse_gate_log(sv._read(path))
            except OSError:
                continue
            if not parsed["label"] or not parsed["blocks"] or ended < started:
                continue
            gates.append({"started": started, "ended": ended, "label": parsed["label"],
                          "detail": " ".join(parsed["verdict"])[0:300]})
        for path in unit_paths:
            started = gate_log_started(Path(path.name.replace("testfast-unit-", "gates-")))
            if started is None:
                continue
            try:
                tail = sv._read(path)[-3000:]
                path.stat()
            except OSError:
                continue
            hit = None
            for hit in sv._UNIT_RESULT.finditer(tail):
                pass
            if hit is None:
                continue
            ended = started + float(hit.group(3))
            if ended >= started:
                units.append({"started": started, "ended": ended})
    return {"gates": gates, "units": units}


class GateLogRef:
    """A gate's master log and where its sibling artifacts (declarations, evidence) are named from.

    A log another box sent (`box` set) carries its own `text` and `mtime`: its path is that box's, and
    nothing beside it is read on this one.
    """

    def __init__(self, path: Path, started: float | None, art_dir: Path | None = None, art_stem: str | None = None,
                 *, text: str | None = None, mtime: float | None = None, box: str = ""):
        self.path = path
        self.started = started
        self.art_dir = art_dir or path.parent
        self.art_stem = art_stem or path.stem
        self.text = text
        self.box = box
        self._mtime = mtime

    def read(self) -> str:
        return self.text if self.text is not None else sv._read(self.path)

    def mtime(self) -> float | None:
        if self.box:
            return self._mtime
        try:
            return self.path.stat().st_mtime
        except OSError:
            return None


def _as_ref(value: Path | GateLogRef | None) -> GateLogRef | None:
    if value is None or isinstance(value, GateLogRef):
        return value
    return GateLogRef(value, gate_log_started(value))


def _live_stdout_log(procs: list[dict], label: str, now: float) -> GateLogRef | None:
    """The running gate_run's own stdout, when it is a file: its console log, written as the run goes."""
    for proc in procs:
        argv = sv._argv(proc["args"])
        script = next((a for a in argv if a.endswith("gate_run.py")), None)
        if not script or "--label" not in argv or argv[argv.index("--label") + 1:][:1] != [label]:
            continue
        try:
            target = Path(os.readlink(f"/proc/{proc['pid']}/fd/1"))
            if not target.is_file() or f"gate run {label}" not in sv._read(target)[:400]:
                continue
        except OSError:
            continue
        tree_logs = Path(script).resolve().parents[1] / "test-logs"
        stems = []
        try:
            stems = sorted(tree_logs.glob("gates-*-declaration-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            pass
        stem = stems[0].name.split("-declaration-")[0] if stems else None
        return GateLogRef(target, now - proc["elapsed"], tree_logs if stem else None, stem)
    return None


def find_gate_log(procs: list[dict], repo: Path, memory: Any, label: str | None,
                  now: float | None = None) -> GateLogRef | None:
    """The newest master log of gate `label`: the running gate's stdout file first, else the newest
    `test-logs/gates-*.log` in its own tree, the last tree seen, or the repo."""
    if label is not None:
        live = _live_stdout_log(procs, label, time.time() if now is None else now)
        if live is not None:
            return live
    roots: list[Path] = []
    for proc in procs:
        argv = sv._argv(proc["args"])
        script = next((a for a in argv if a.endswith("gate_run.py")), None)
        if script and "--label" in argv:
            roots.append(Path(script).resolve().parents[1])
    if memory is not None and memory.gate_root:
        roots.append(Path(memory.gate_root))
    for named in os.environ.get("SESSIONVIEW_WEB_GATE_ROOTS", "").split(os.pathsep):
        if named.strip():
            roots.append(Path(named.strip()))
    roots.append(repo)
    roots += sv._scratch_roots()
    seen: set[Path] = set()
    best: tuple[float, Path, Path] | None = None
    for root in roots:
        if root in seen:
            continue
        seen.add(root)
        try:
            logs = sorted((root / "test-logs").glob("gates-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            continue
        for path in logs[:12]:
            first = sv._read(path)[:200].splitlines()[:1]
            if first and first[0].startswith("gate run ") and (label is None or first[0].strip() == f"gate run {label}"):
                if label is not None:
                    if memory is not None:
                        memory.set_gate_root(str(root))
                    return _as_ref(path)
                if best is None or path.stat().st_mtime > best[0]:
                    best = (path.stat().st_mtime, path, root)
                break
    if best is not None:
        if memory is not None:
            memory.set_gate_root(str(best[2]))
        return _as_ref(best[1])
    return None


def _scratch_roots() -> list[Path]:
    """Checkouts a session built a batch tree in, where its gate log lands: the newest six the setting's glob names.

    `SESSIONVIEW_WEB_SCRATCH_GLOB` is an absolute glob with no default (ADR-0002);
    unset, no scratch checkout is searched and `SESSIONVIEW_WEB_GATE_ROOTS` plus
    the live `gate_run.py` path are the only roots.
    """
    named = os.environ.get("SESSIONVIEW_WEB_SCRATCH_GLOB", "").strip()
    if not named or not Path(named).is_absolute():
        return []
    anchor = Path(Path(named).anchor)
    try:
        return sorted(anchor.glob(str(Path(named).relative_to(anchor))),
                      key=lambda p: (p / "test-logs").stat().st_mtime if (p / "test-logs").is_dir() else 0,
                      reverse=True)[:6]
    except (OSError, ValueError):
        return []
