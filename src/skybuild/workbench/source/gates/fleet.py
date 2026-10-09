"""The Fleet section: every box's row, its processes, its seams by agent trailer, and the probe that
runs on a remote box over ssh (`SESSIONVIEW_WEB_REMOTE_SSH=off` switches it off).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import hub as sv
from .keeper import read_journal

if TYPE_CHECKING:      # names used in annotations only (strings at runtime)
    from .keeper import JournalReader
    from .processes import Runner


def fleet_row(box: str, scan: dict | None, problem: str = "", sub_agents: str = "not visible by ps",
              keeper: dict | None = None, utilisation: dict | None = None) -> dict:
    if scan is None:
        return {"box": box, "claude": "-", "claude_headless": "-", "codex": "-", "grok": "-", "sub_agents": "-",
                "sub_processes": "-", "working_on": "-", "keepers_and_loops": "-",
                **sv.keeper_columns(None, "the box was not read"), "idle_with_free_memory": "not read",
                "note": problem or "not read"}

    def many(rows: list[dict]) -> str:
        return f"{len(rows)} (longest {sv._elapsed(max(r['elapsed'] for r in rows))})" if rows else "0"
    keepers = "; ".join(f"{k['name']}{' ' + k['stream'] if k['stream'] else ''} {sv._elapsed(k['elapsed'])}"
                        f" ({k.get('children', 0)} sub-processes)"
                        for k in sorted(scan["keepers"], key=lambda k: -k["elapsed"])) or "none"
    work = [f"{kind} {row['pid']} in {Path(row['cwd']).name}" + (f" on {row['branch']}" if row.get("branch") else "")
            for kind, row in sv._roots(scan) if kind != "keepers" and row.get("cwd") and row["cwd"] != str(Path.home())]
    row = {"box": box, "claude": many(scan["claude"]),
            "claude_headless": many(scan["claude_headless"]) + " (started by a keeper or loop)" if scan["claude_headless"] else "0",
            "codex": many(scan["codex"]), "grok": many(scan["grok"]), "sub_agents": sub_agents,
            "sub_processes": sum(row.get("children", 0) for _, row in sv._roots(scan)),
            "working_on": "; ".join(work) or "no agent sits in a checkout", "keepers_and_loops": keepers,
            **sv.keeper_columns(keeper), "idle_with_free_memory": ("not read" if not utilisation else
                utilisation.get("error") or f"{sum(h['idle_minutes'] for h in utilisation.get('hours', []))} min / 24 h"),
            "note": problem or (utilisation.get("error", "") if utilisation else "")}
    if utilisation:
        row["charts"] = {f"{box}: idle minutes per hour": _utilisation_svg(utilisation)}
    return row


UTILISATION_WINDOW = 24 * 60 * 60
UTILISATION_CAP = 24 * 60


class BoxUtilisationLog:
    """One sample per minute, kept for 24 hours; unreadable records are never overwritten."""
    def __init__(self, path: Path | None = None):
        self.path = path or sv.default_warning_file().with_name("box_utilisation.json")
        self.samples: list[dict] = []
        self.error = ""
        try:
            value = json.loads(self.path.read_text())
            if not isinstance(value, dict) or not isinstance(value.get("samples"), list):
                raise TypeError("unexpected ring shape")
            self.samples = [s for s in value["samples"] if isinstance(s, dict) and isinstance(s.get("at"), (int, float))]
        except (FileNotFoundError, NotADirectoryError):
            pass
        except (OSError, ValueError, TypeError) as exc:
            self.error = f"utilisation ring file could not be read ({type(exc).__name__})"

    def add(self, sample: dict) -> None:
        if self.error:
            return
        at = float(sample["at"])
        if self.samples and at - float(self.samples[-1]["at"]) < 60:
            return
        self.samples = [s for s in self.samples if at - float(s["at"]) < UTILISATION_WINDOW]
        self.samples.append(sample)
        self.samples = self.samples[-UTILISATION_CAP:]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"samples": self.samples}, separators=(",", ":")))
        except OSError as exc:
            self.error = f"utilisation ring file could not be written ({type(exc).__name__})"

    def chart(self, now: float) -> dict:
        if self.error:
            return {"hours": [], "error": self.error}
        start = int(now // 3600) * 3600 - 23 * 3600
        hours = []
        for hour in range(start, start + 24 * 3600, 3600):
            rows = [s for s in self.samples if hour <= float(s["at"]) < hour + 3600]
            idle = sum(1 for s in rows if s.get("agents") == 0 and isinstance(s.get("available_gib"), (int, float))
                       and isinstance(s.get("reserve_gib"), (int, float)) and s["available_gib"] > s["reserve_gib"])
            hours.append({"at": hour, "idle_minutes": idle})
        return {"hours": hours, "error": "", "current": self.samples[-1] if self.samples else None}


def _box_utilisation(scan: dict, procs: list[dict], now: float) -> dict:
    """Sample this box's memory, load and live keeper reserve; unreadable proc data is unknown."""
    try:
        mem = Path("/proc/meminfo").read_text()
        available = re.search(r"^MemAvailable:\s+(\d+)\s+kB$", mem, re.MULTILINE)
        load = float(Path("/proc/loadavg").read_text().split()[0])
        if not available:
            raise ValueError("MemAvailable absent")
        reserve = None
        for proc in procs:
            argv = sv._argv(proc["args"])
            if any("session_keeper.py" in a for a in argv):
                at = argv.index("--min-mem-gib") if "--min-mem-gib" in argv else -1
                if at >= 0 and at + 1 < len(argv):
                    reserve = float(argv[at + 1]); break
        agents = sum(len(scan.get(k, [])) for k in ("claude", "claude_headless", "codex", "grok"))
        sample = {"at": now, "agents": agents, "available_gib": int(available.group(1)) / 1024 / 1024,
                  "reserve_gib": reserve, "load": load}
    except (OSError, ValueError, IndexError):
        return {"error": "box memory/load could not be read", "hours": []}
    ring = BoxUtilisationLog()
    ring.add(sample)
    return ring.chart(now)


def _utilisation_svg(value: dict) -> dict | None:
    hours = value.get("hours", [])
    if not hours:
        return None
    points = " ".join(f"{10 + i * 3.4:.1f},{40 - min(30, h['idle_minutes'] / 2)}" for i, h in enumerate(hours))
    return {"tag": "svg", "attrs": {"viewBox": "0 0 90 44", "role": "img", "aria-label": "idle minutes per hour"},
            "children": [{"tag": "polyline", "attrs": {"points": points, "fill": "none", "stroke": "currentColor",
                          "stroke-width": "1.5"}, "children": []}]}


def fleet_procs(box: str, scan: dict | None) -> list[dict]:
    """The command line of every agent, keeper and loop, and of what each one spawned (helper shells left out)."""
    rows = []
    for kind, root in sv._roots(scan or {}):
        rows.append({"box": box, "process": kind, "pid": root["pid"], "up": sv._elapsed(root["elapsed"]),
                     "command": root.get("cmd", ""), "working_dir": root.get("cwd", ""), "branch": root.get("branch", ""),
                     "sub_processes": root.get("children", 0)})
        rows += [{"box": box, "process": f"  child of {root['pid']:d}", "pid": t["pid"], "up": sv._elapsed(t["elapsed"]),
                  "command": t["cmd"], "working_dir": "", "branch": "", "sub_processes": ""} for t in root.get("tree", [])]
    return rows or [{"box": box, "process": "none", "pid": "", "up": "", "command": "", "working_dir": "",
                     "branch": "", "sub_processes": ""}]


#: What the fleet box says when no other box is named.
FLEET_BOXES_UNSET = "SESSIONVIEW_WEB_FLEET_BOXES unset: only this box is shown"


def fleet_boxes() -> list[str]:
    """The other boxes the fleet view reads, from `SESSIONVIEW_WEB_FLEET_BOXES` (commas or spaces).

    No default (ADR-0002): unset or empty means this box only, so no ssh probe
    runs and no box is named to `seams_by_box`.
    """
    return os.environ.get("SESSIONVIEW_WEB_FLEET_BOXES", "").replace(",", " ").split()


def fleet_section(runner: Runner, remote: RemoteProbe | None, now: float, local_subagents: str = "",
                  journal: JournalReader = read_journal) -> dict:
    """Rows per box (this box from `ps` and its keeper journal, the others from the ssh probe) and every box's
    process command lines."""
    node = os.uname().nodename
    others = fleet_boxes()
    local_procs = sv.read_processes(runner)
    local_scan = sv.fleet_scan(local_procs)
    boxes = [(node + " (this box)", local_scan, "" if others else FLEET_BOXES_UNSET,
              sv.keeper_idle(journal), _box_utilisation(local_scan, local_procs, now))]
    for box in others:
        got = remote.get(box, now) if remote is not None else {"problem": "no ssh probe wired"}
        data = got.get("data") or {}
        scan = data.get("fleet")
        boxes.append((box, scan, got.get("problem", "" if scan else "the box's probe sent no fleet data"),
                      data.get("keeper"), data.get("utilisation")))
    return {"boxes": [fleet_row(b, sc, pr, local_subagents or "not visible by ps", kp, u) if b.endswith("(this box)")
                      else fleet_row(b, sc, pr, keeper=kp, utilisation=u) for b, sc, pr, kp, u in boxes],
            "procs": [row for b, sc, _, _, _ in boxes for row in fleet_procs(b, sc)]}


def stream_boxes(names: Sequence[str], known: Sequence[str]) -> dict[str, str]:
    """{stream: box} from the stream files' names (`W8-wonko.md` -> W8: wonko); a stream naming no known box is absent."""
    out = {}
    for name in names:
        m = re.match(r"(W\d+)-([a-z0-9]+)", Path(name).name.lower().replace("w", "W", 1))
        if m and m.group(2) in known:
            out[m.group(1)] = m.group(2)
    return out


def seams_by_box(repo: Path, runner: Runner, known: Sequence[str], per_box: int = 5) -> list[dict]:
    """The last `per_box` seams each box worked, merged ones (done) and pushed unmerged ones (waiting), by Agent trailer."""
    ref = sv.trunk_ref(repo, runner)
    node = os.uname().nodename.lower()
    owners = stream_boxes(runner(["git", "-C", str(repo), "ls-tree", "--name-only", ref, "docs/dev/streams/"]).splitlines(),
                          [*known, node])
    found: dict[str, dict] = {}
    for line in runner(["git", "-C", str(repo), "for-each-ref", "--sort=-committerdate", "--count=60", ("--format="
                        "%(committerdate:unix)\t%(refname:short)\t%(trailers:key=Agent,valueonly,separator=%x3b)"),
                        "refs/remotes/origin/seam"]).splitlines():
        at, name, agent = (line.split("\t") + ["", ""])[:3]
        if at.isdigit() and "/" in name:
            seam = name.split("seam/", 1)[-1]
            found[seam] = {"seam": seam, "at": int(at), "state": "pushed, waiting for the integrator", "agent": agent.strip()}
    merges = [l.split("\t", 2) for l in runner(["git", "-C", str(repo), "log", ref, "--first-parent", "--merges", "-120",
                                               "--format=%ct\t%P\t%s"]).splitlines()]
    tips = {}
    for at, parents, subject in ((m + ["", "", ""])[:3] for m in merges):
        seam = subject.split("seam/", 1)[-1].split()[0] if "seam/" in subject else ""
        second = parents.split()[1:2]
        if seam and at.isdigit() and second:
            tips[second[0]] = (seam, int(at))
    if tips:
        trailers = {}
        for line in runner(["git", "-C", str(repo), "show", "-s", "--format=%H\t%(trailers:key=Agent,valueonly,separator=%x3b)",
                            *tips]).splitlines():
            sha, _, agent = line.partition("\t")
            trailers[sha] = agent.strip()
        for sha, (seam, at) in tips.items():
            found[seam] = {"seam": seam, "at": at, "state": "merged (done)", "agent": trailers.get(sha, "")}
    by_box: dict[str, list[dict]] = {}
    for row in found.values():
        stream = row["agent"].split("/", 1)[0] if row["agent"] else ""
        box = owners.get(stream) or ("this box" if stream == "owner" else (f"{stream} (box unknown)" if stream else "unknown"))
        by_box.setdefault(box, []).append(row)
    out = []
    for box in sorted(by_box):
        for row in sorted(by_box[box], key=lambda r: -r["at"])[:per_box]:
            out.append({"box": box, "seam": row["seam"], "state": row["state"], "when": sv.zoned(row["at"], "%m-%d %H:%M"),
                        "agent": row["agent"] or "no Agent trailer"})
    return out


def remote_probe() -> dict:
    """Runs ON a box (the module's source is piped to its python): its gate slices' lanes as `gate_section` shows them,
    its fleet scan, and its keeper's idle summary over the window the page's box set (`_keeper_window_source`).

    Read-only: it reads `ps`, lane logs, `.out` files and the keeper's journal, and prints one JSON document.
    """
    now = time.time()
    procs = sv.read_processes()
    bases = sv.slice_labels(procs)
    dirs = sv._default_state_dirs()
    sections = {}
    for base in sorted(bases):
        got = sv.gate_section(procs, dirs, now, base)
        if got is not None:
            sections[base] = got
    # This box's newest gate for the panel's top: its own gate_run's, else a slice it runs for another
    # box's gate (marked so it never outranks that gate), else the newest finished one in its lane logs.
    running = sv.find_gate_run(procs)
    if running is not None:
        gate = sections.get(running["label"]) or sv.gate_section(procs, dirs, now)
    elif sections:
        gate = dict(max(sections.values(), key=lambda sec: sec.get("started_at") or 0.0), slice=True)
    else:
        gate = sv.gate_section(procs, dirs, now)
    report = gate_report(gate["label"]) if gate is not None and gate["state"] == "finished" else None
    scan = sv.fleet_scan(procs)
    return {"sections": sections, "gate": gate, "gate_report": report, "fleet": scan,
            "utilisation": _box_utilisation(scan, procs, now),
            "keeper": sv.keeper_idle(), "at": now, "host": os.uname().nodename}


def _report_files() -> list[Path]:
    """The files `SESSIONVIEW_WEB_GATE_REMOTE_REPORTS` names on this box, newest first; unset names none."""
    found: set[Path] = set()
    for pattern in os.environ.get(sv.GATE_REPORTS_VAR, "").split(os.pathsep):
        pattern = pattern.strip()
        if not pattern or not Path(pattern).is_absolute():
            continue
        anchor = Path(Path(pattern).anchor)
        try:
            found |= {p for p in anchor.glob(str(Path(pattern).relative_to(anchor))) if p.is_file()}
        except (OSError, ValueError):
            continue

    def mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0
    return sorted(found, key=mtime, reverse=True)


def gate_report(label: str) -> dict | None:
    """Gate `label`'s console report on this box, from its `gate run <label>` line on: {path, mtime, started, text}.

    gate_run prints its report when the run ends and writes it to its tree's `test-logs/`; a tree is often
    removed once its batch lands, and the run's own stdout file is what is left. None when no named file
    holds it.
    """
    head = re.compile(rf"^gate run {re.escape(label)}[ \t]*$", re.MULTILINE)
    for path in _report_files()[:sv.REPORT_FILES_SCANNED]:
        try:
            with path.open(errors="replace") as handle:
                text = handle.read(sv.REPORT_READ_CAP)
            mtime = path.stat().st_mtime
        except OSError:
            continue
        hit = head.search(text)
        if hit is None:
            continue
        body = text[hit.start():][:sv.REPORT_SEND_CAP]
        written = sv._REPORT_WRITTEN.search(body)
        return {"path": str(path), "mtime": mtime, "text": body,
                "started": sv.gate_log_started(Path(written.group(1))) if written else sv.gate_log_started(path)}
    return None


def _gate_reports_source() -> str:
    """Python a box runs ahead of `remote_probe()`: this box's report glob(s), as one literal, or unset there too."""
    raw = os.environ.get(sv.GATE_REPORTS_VAR)
    set_it = (f"_o.environ[{sv.GATE_REPORTS_VAR!r}] = {raw!r}" if raw is not None
              else f"_o.environ.pop({sv.GATE_REPORTS_VAR!r}, None)")
    return f"\nimport os as _o\n{set_it}\n"


class RemoteProbe:
    """Reads the other boxes' running lanes over ssh in a background thread; the page never waits on it."""

    def __init__(self, runner: Callable[..., Any] | None = None) -> None:
        self.lock = threading.Lock()
        self.cache: dict[str, dict] = {}
        self.running: set[str] = set()
        self.until: dict[str, float] = {}
        self.runner = runner or subprocess.run
        self.spawn = lambda fn: threading.Thread(target=fn, daemon=True).start()

    def get(self, box: str, now: float) -> dict:
        """The last result for `box` ({data, at} or {problem}); a stale one starts a background refresh."""
        if not re.fullmatch(r"\w+", box):
            return {"problem": "box name not usable for ssh"}
        if os.environ.get("SESSIONVIEW_WEB_REMOTE_SSH", "").strip().lower() == "off":
            return {"problem": "the ssh probe is switched off (SESSIONVIEW_WEB_REMOTE_SSH=off)"}
        launch = False
        with self.lock:
            entry = dict(self.cache.get(box, {}))
            stale = not entry or now - entry.get("at", 0) >= sv.REMOTE_PROBE_TTL_SECONDS
            if stale and box not in self.running and now >= self.until.get(box, 0):
                self.running.add(box)
                launch = True
        if launch:
            self.spawn(lambda: self._fetch(box))   # outside the lock: the fetch takes it to store its answer
            with self.lock:
                entry = dict(self.cache.get(box, {})) or entry
        if not entry:
            return {"problem": sv.PROBE_PENDING} if box in self.running else \
                   {"problem": "no ssh probe yet (backing off after a failure)"}
        return entry

    def _fetch(self, box: str) -> None:
        source = (sv.remote_source() + sv._keeper_window_source() + _gate_reports_source()
                  + "\nimport json as _j\nprint(_j.dumps(remote_probe()))\n")
        entry: dict[str, Any]
        try:
            done = self.runner(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", "-o", "StrictHostKeyChecking=yes",
                                box, "python3", "-"], input=source, capture_output=True, text=True,
                               timeout=sv.REMOTE_PROBE_TIMEOUT_SECONDS, check=False)
            if done.returncode != 0:
                tail = (done.stderr or "").strip().splitlines()[-1:] or ["no message"]
                raise RuntimeError(f"ssh exited {done.returncode}: {sv._redact(tail[0], 160)}")
            entry = {"data": json.loads(done.stdout), "at": time.time()}
        except subprocess.TimeoutExpired:
            entry = {"problem": f"ssh to {box} did not answer in {int(sv.REMOTE_PROBE_TIMEOUT_SECONDS)}s (it may be waiting "
                                f"on an authentication prompt); not retried for {int(sv.REMOTE_PROBE_BACKOFF_SECONDS // 60)} minutes",
                     "at": time.time()}
            with self.lock:
                self.until[box] = time.time() + sv.REMOTE_PROBE_BACKOFF_SECONDS
        except (RuntimeError, ValueError, OSError) as exc:
            # Every reader (fleet row, gate boxes, warning file) shows this text: no credential reaches it.
            entry = {"problem": f"ssh probe of {box} failed: {sv._redact(str(exc), 200)}", "at": time.time()}
            with self.lock:
                self.until[box] = time.time() + sv.REMOTE_PROBE_BACKOFF_SECONDS
        with self.lock:
            self.cache[box] = entry
            self.running.discard(box)
