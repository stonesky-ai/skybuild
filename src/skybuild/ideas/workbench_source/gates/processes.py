"""The processes on this box as `ps` lists them, and the running gate: `gate_run.py --label <batch>`
and each `gate_lane.sh` lane with the suite its pytest child is inside.
"""
from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from . import hub as sv

Runner = Callable[[Sequence[str]], str]

#: Past runs of one suite that set its typical duration (a median of the newest).
HISTORY_DEPTH = 7
#: The history scan reads every lane log; it is re-read at most this often.
HISTORY_TTL_SECONDS = 300.0

_SUITE_LINE = re.compile(r"^SUITE\s+(\S+)\s+rc=(-?\d+)\s*(.*)$")
_DURATION = re.compile(r"\bin\s+([0-9]+(?:\.[0-9]+)?)s\b")
_LEDGER = re.compile(r"^Batch(\d+) ledger")
#: The ledger commit of a tooling batch: it lands rows with no gate and carries no batch number. It bounds a
#: batch as a numbered ledger does, or its merges would be counted into the batch after it.
_TOOLING_LEDGER = re.compile(r"^Tooling batch ledger")
_MERGE = re.compile(r"^Merge seam/(\S+)")
_REVISION = re.compile(r"revision line|\bHOLD\b")
_UNIT_RESULT = re.compile(r"(\d+) passed(?:, (\d+) skipped)?.* in ([0-9.]+)s")
_UNIT_FAILED = re.compile(r"(\d+) failed")

_history_cache: dict[str, Any] = {"at": 0.0, "key": None, "value": {}}


def run_text(argv: Sequence[str]) -> str:
    """Stdout of `argv`, or "" when it cannot run (a missing source is a note, not a crash)."""
    try:
        done = subprocess.run(list(argv), capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout if done.returncode == 0 else ""


def fmt_seconds(value: float | None) -> str:
    if value is None:
        return "unknown"
    seconds = int(max(0, round(value)))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


# ---------------------------------------------------------------- processes

def read_processes(runner: Runner = run_text) -> list[dict]:
    """Every process as {pid, ppid, elapsed, args}, from one `ps`."""
    rows = []
    for line in runner(["ps", "-eo", "pid=,ppid=,etimes=,args="]).splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4 or not all(p.isdigit() for p in parts[:3]):
            continue
        rows.append({"pid": int(parts[0]), "ppid": int(parts[1]),
                     "elapsed": int(parts[2]), "args": parts[3]})
    return rows


def _argv(args: str) -> list[str]:
    return args.split()


def _is_pytest(argv: list[str]) -> bool:
    """`pytest …`, `python -m pytest …`, `uv run pytest …`: a test run, by its first words only."""
    return any(a.rsplit("/", 1)[-1] in ("pytest", "py.test") for a in argv[:4])


def _under_pytest(proc: dict, by_pid: dict[int, dict]) -> bool:
    """True for a process a test started: a pytest run is among its ancestors.

    Tests of the gate tooling start `gate_lane.sh` and `gate_run.py` themselves, with labels such as
    `probe` or `alone`. Read as a gate, such a script hid the real run behind a pass with no suites in it.
    A real gate's lane runs pytest below it, never above it.
    """
    seen: set[int] = set()
    pid = proc.get("ppid")
    while pid and pid not in seen and len(seen) < 64:
        seen.add(pid)
        parent = by_pid.get(pid)
        if parent is None:
            return False
        if _is_pytest(_argv(parent["args"])):
            return True
        pid = parent.get("ppid")
    return False


def slice_labels(procs: list[dict]) -> set[str]:
    """The batch labels of the gate lane scripts running on this box (a test's own lane scripts left out)."""
    by_pid = {p["pid"]: p for p in procs}
    bases = set()
    for proc in procs:
        argv = _argv(proc["args"])
        at = next((i for i, a in enumerate(argv) if a.endswith("gate_lane.sh")), None)
        if at is not None and len(argv) > at + 2 and argv[at + 1].isdigit() and not _under_pytest(proc, by_pid) \
                and not sv.is_teardown_label(argv[at + 2]):
            bases.add(re.sub(r"(-restarts|-alone|-rerun-.+)$", "", argv[at + 2]))
    return bases


def find_gate_run(procs: list[dict]) -> dict | None:
    """The running distributed gate: label and age, or None (a gate_run.py a test started is none)."""
    by_pid = {p["pid"]: p for p in procs}
    for proc in procs:
        argv = _argv(proc["args"])
        if any(a.endswith("gate_run.py") for a in argv) and "--label" in argv and not _under_pytest(proc, by_pid):
            at = argv.index("--label") + 1
            if at < len(argv):
                return {"label": argv[at], "elapsed": proc["elapsed"], "pid": proc["pid"]}
    return None


#: A running suite whose output file has not changed for this long is warned as possibly stuck.
QUIET_WARN_SECONDS = 300


def _warn(code: str, subject: str, text: str) -> dict:
    return {"code": code, "subject": subject, "text": text}


def phase_of(label: str, base: str) -> str | None:
    """The phase a lane-script label belongs to for batch `base`, or None for another batch."""
    for suffix, phase in sv._PHASE_SUFFIX.items():
        if label == base + suffix:
            return phase
    if label.startswith(base) and sv._RERUN.fullmatch(label[len(base):]):
        return "rerun"
    return None


def lane_plans(procs: list[dict], base: str) -> dict[tuple[str, int, str], dict]:
    """(phase, lane, label) -> {suites, pid, elapsed, running} from the live `gate_lane.sh <lane> <label> <suites>`.

    A lane script that is the child of another lane script is the suite pass
    re-entering itself, not a second lane: the oldest one is the lane.
    """
    lanes: dict[tuple[str, int, str], dict] = {}
    by_pid = {p["pid"]: p for p in procs}
    for proc in procs:
        argv = _argv(proc["args"])
        at = next((i for i, a in enumerate(argv) if a.endswith("gate_lane.sh")), None)
        if at is None or len(argv) < at + 3 or not argv[at + 1].isdigit() \
                or phase_of(argv[at + 2], base) is None or _under_pytest(proc, by_pid):
            continue
        key = (phase_of(argv[at + 2], base), int(argv[at + 1]), argv[at + 2])
        suites = [a for a in argv[at + 3:] if a.endswith(".py")]
        known = lanes.get(key)
        if known is None or proc["elapsed"] > known["elapsed"]:
            lanes[key] = {"suites": suites, "pid": proc["pid"], "elapsed": proc["elapsed"],
                          "label": argv[at + 2]}
    for info in lanes.values():
        info["running"] = _running_suite(by_pid, info["pid"], procs)
    return lanes



def _running_suite(by_pid: dict[int, dict], lane_pid: int, procs: list[dict]) -> dict | None:
    """The `pytest <suite>` process under a lane script, with its elapsed seconds."""
    descend = {lane_pid}
    for _ in range(8):
        descend |= {p["pid"] for p in procs if p["ppid"] in descend}
    best = None
    for pid in descend:
        argv = _argv(by_pid[pid]["args"]) if pid in by_pid else []
        if "pytest" not in " ".join(argv):
            continue
        for arg in argv:
            if arg.startswith("tests/") and arg.endswith(".py"):
                elapsed = by_pid[pid]["elapsed"]
                if best is None or elapsed > best["elapsed"]:
                    best = {"suite": arg, "elapsed": elapsed}
    return best


def lane_state_dir(pid: int) -> Path | None:
    """SKYKEEP_LANE_STATE_DIR from a lane script's own environment (same user, readable)."""
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return None
    for item in raw.split(b"\0"):
        if item.startswith(b"SKYKEEP_LANE_STATE_DIR="):
            return Path(item.split(b"=", 1)[1].decode(errors="replace"))
    return None
