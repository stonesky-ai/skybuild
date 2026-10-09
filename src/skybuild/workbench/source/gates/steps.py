"""Integration steps that stay: `steps.json`, the live and final evidence of each step, and `build_steps`.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from . import hub as sv

if TYPE_CHECKING:      # names used in annotations only (strings at runtime)
    from .gate_log import GateLogRef


class StepMemory:
    """What was seen live and leaves no durable trace: a merge or pass observed running, kept with its times.

    Written beside the warning file (SESSIONVIEW_WEB_STEPS_FILE overrides). A write that fails is
    said in `problem`, never raised.
    """

    def __init__(self, path: Path | None = None) -> None:
        named = os.environ.get("SESSIONVIEW_WEB_STEPS_FILE", "").strip()
        self.path = path or (Path(named) if named else
                             Path.home() / ".local" / "state" / "skykeep-sessionview" / "steps.json")
        self.lock = threading.Lock()
        self.observed: dict[str, dict] = {}
        self.gate_root = ""
        #: The tree the batch was last found being built in (batch.batch_tree); kept for this run only.
        self.batch_tree = ""
        self.problem = ""
        self._written = 0.0
        try:
            data = json.loads(self.path.read_text())
            self.observed = {k: v for k, v in (data.get("observed") or {}).items() if isinstance(v, dict)}
            self.gate_root = str(data.get("gate_root") or "")
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError) as exc:
            self.problem = f"the steps file could not be read ({type(exc).__name__}): starting empty"

    def set_gate_root(self, root: str) -> None:
        if root != self.gate_root:
            self.gate_root = root
            self._save(0.0)

    def observe(self, live: Sequence[tuple[str, str]], now: float) -> None:
        """`live` = (key, label) of every step running right now; a key no longer live is closed."""
        with self.lock:
            changed = False
            keys = set()
            for key, label in live:
                keys.add(key)
                entry = self.observed.get(key)
                if entry is None or entry.get("ended"):
                    self.observed[key] = {"label": label, "first": now, "last": now, "ended": None}
                    changed = True
                else:
                    entry["last"] = now
            for key, entry in self.observed.items():
                if key not in keys and not entry.get("ended"):
                    entry["ended"] = entry.get("last", now)
                    changed = True
            if len(self.observed) > sv.OBSERVED_CAP:
                for key in sorted(self.observed, key=lambda k: self.observed[k].get("last", 0))[:len(self.observed) - sv.OBSERVED_CAP]:
                    del self.observed[key]
                changed = True
            if changed or (self.observed and now - self._written >= sv.WARNING_WRITE_SECONDS):
                self._save(now)

    def _save(self, now: float) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps({"observed": self.observed, "gate_root": self.gate_root}, indent=1))
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
            self._written = now
            self.problem = ""
        except OSError as exc:
            self.problem = f"could not write the steps file ({type(exc).__name__}): live steps are kept in memory only"

    def started_of(self, text: str) -> float | None:
        """When a live observation whose label names `text` was first seen (the earliest such)."""
        with self.lock:
            firsts = [e["first"] for e in self.observed.values() if text and text in e.get("label", "")]
        return min(firsts) if firsts else None


#: The name of the step that runs `scripts/skykeep.sh testfast` on the merged batch. It was "unit/adversarial
#: lane", which told the owner nothing (2026-10-06): the step is the fast tests, and STEP_MEANINGS says what they are.
FAST_TESTS = "fast tests (testfast)"
#: What each kind of step is, in one sentence, for the page's "What each step is" list. The key is the
#: start of a step's name.
STEP_MEANINGS: tuple[tuple[str, str], ...] = (
    ("merge ", "A seam (one row's branch) merged into the batch with `--no-ff`."),
    (FAST_TESTS, "`scripts/skykeep.sh testfast` on the merged batch: every test that needs no running vault "
                 "(the unit tests under src/, and tests/adversarial, tests/bdd, tests/fakes, tests/benchmark), "
                 "about 28,000 tests in about 15 minutes, run in parallel on this box. It starts no containers "
                 "and uses no model. Red here means a merged seam broke a test, or the run's own environment did."),
    ("restart wave", "The gate's disposable vault stacks are started, one per lane."),
    ("wave pass", "The gate suites (tests/gates) run against real vault stacks, several lanes at once."),
    ("remote slice", "The part of the gate suites another box runs, to share the load."),
    ("alone phase", "The gate suites that need the whole box to themselves, run one at a time."),
    ("re-run alone", "A suite that was red in the wave, run again alone: red twice is a real red."),
    ("ledger commit", "The commit that deletes the landed rows' briefs and closes the batch; then the push."),
)
_FAILED_LINE = re.compile(r"^(?:FAILED|ERROR) (\S+)(?: - (.*))?$", re.MULTILINE)
_FAILURE_HEAD = re.compile(r"^_{3,} (.+?) _{3,}$", re.MULTILINE)
#: How much of a fast-test log's end is read for its failures, how many are sent, how long a reason may be.
FAILURE_TAIL_BYTES, FAILURES_SENT, REASON_CHARS = 4_000_000, 60, 300
_failure_cache: dict[str, tuple[tuple[int, int], list[dict]]] = {}


def step_meaning(name: object) -> str:
    return next((means for start, means in STEP_MEANINGS if str(name).startswith(start)), "")


def unit_failures(text: str) -> list[dict]:
    """The tests a pytest log says failed, each with the first line of its error, as plain text.

    The short summary names each failed test (`FAILED path::test`, with ` - reason` when pytest prints
    one); the failure sections above it hold the error lines (`E   ...`). A test whose section cannot be
    found gets an empty reason, never a guessed one.
    """
    reasons: dict[str, str] = {}
    heads = list(_FAILURE_HEAD.finditer(text))
    for i, head in enumerate(heads):
        block = text[head.end():heads[i + 1].start() if i + 1 < len(heads) else len(text)]
        error = next((line[1:].strip() for line in block.splitlines() if line.startswith("E ") and line[1:].strip()), "")
        if error:
            reasons.setdefault(head.group(1).strip(), error)
    found, seen = [], set()
    for hit in _FAILED_LINE.finditer(text):
        test = hit.group(1)
        if test in seen:
            continue
        seen.add(test)
        name = test.split("::", 1)[1].replace("::", ".") if "::" in test else test
        reason = (hit.group(2) or reasons.get(name) or reasons.get(name.split("[", 1)[0]) or "").strip()
        found.append({"test": test, "reason": reason[:REASON_CHARS]})
    return found


def _log_failures(path: Path) -> list[dict]:
    """`unit_failures` of a log's end, read once per version of the file."""
    try:
        stat = path.stat()
        key = (stat.st_mtime_ns, stat.st_size)
        kept = _failure_cache.get(str(path))
        if kept is not None and kept[0] == key:
            return kept[1]
        with path.open("rb") as handle:
            handle.seek(max(0, stat.st_size - FAILURE_TAIL_BYTES))
            text = handle.read().decode("utf-8", "replace")
    except OSError:
        return []
    found = unit_failures(text)
    if len(_failure_cache) > 32:
        _failure_cache.clear()
    _failure_cache[str(path)] = (key, found)
    return found


def _unit_runs(repo: Path, procs: list[dict], since: float | None) -> list[dict]:
    steps = []
    try:
        logs = sorted((repo / "test-logs").glob("testfast-unit-*.log"), key=lambda p: p.stat().st_mtime)
    except OSError:
        logs = []
    live = sv._checkout_testfast(repo, procs)
    if since is None:
        logs = logs[-3:]  # no batch start known: the last few lane runs, not every lane log the checkout ever kept
    for path in logs:
        end = path.stat().st_mtime
        if since is not None and end < since:
            continue
        begin = sv.gate_log_started(Path(path.name.replace("testfast-unit-", "gates-")))
        tail = sv._read(path)[-3000:]
        hit = None
        for hit in sv._UNIT_RESULT.finditer(tail):
            pass
        failed = sv._UNIT_FAILED.search(tail)
        result = ("no verdict line (cut off)" if hit is None else
                  f"{hit.group(1)} passed" + (f", {failed.group(1)} FAILED" if failed else ", 0 failed"))
        step = {"step": FAST_TESTS, "detail": result, "state": "done" if hit and not failed else
                ("RED" if failed else "cut off"), "start": begin, "end": end}
        if failed:
            # The tests that failed and why, for the page's list; `run` names the log, which is what a fix row cites.
            failures = _log_failures(path)
            step.update(run=path.name, failures=failures[:FAILURES_SENT], failures_total=len(failures))
        steps.append(step)
    if live and steps and steps[-1]["state"] == "cut off":
        steps[-1].update(state="RUNNING", end=None, detail="running")
    elif live:
        steps.append({"step": FAST_TESTS, "detail": "running", "state": "RUNNING",
                      "start": time.time() - min(p["elapsed"] for p in live), "end": None})
    return steps


def _tree_artifacts(procs: list[dict]) -> tuple[Path, str] | None:
    """Where the running gate's declaration and slice files are: its own tree's test-logs and their stamp."""
    for proc in procs:
        argv = sv._argv(proc["args"])
        script = next((a for a in argv if a.endswith("gate_run.py")), None)
        if script and "--label" in argv:
            tree_logs = Path(script).resolve().parents[1] / "test-logs"
            try:
                found = sorted(tree_logs.glob("gates-*-declaration-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            except OSError:
                return None
            if found:
                return tree_logs, found[0].name.split("-declaration-")[0]
    return None


def _slice_boxes(art: tuple[Path, str] | None) -> dict[str, int]:
    """box -> suites in its slice, from the slice files the gate wrote before it started them."""
    boxes: dict[str, int] = {}
    if art is None:
        return boxes
    for path in art[0].glob(f"{art[1]}-slice-*.json"):
        try:
            data = json.loads(path.read_text())
            boxes[str(data["box"])] = sum(len(lane) for lane in data["lanes"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return boxes


def _slice_suites(art: tuple[Path, str] | None, box: str) -> list[dict]:
    """The suites a remote box was handed, as pending rows: its own progress is not readable from here."""
    if art is None:
        return []
    try:
        data = json.loads((art[0] / f"{art[1]}-slice-{box}.json").read_text())
        return [{"state": "pending", "suite": suite, "text": "handed to the box; its progress is not visible here"}
                for lane in data["lanes"] for suite in lane]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def _final_evidence(path: Path | None) -> bool:
    """True when the gate's copy of a box's evidence holds that box's final record (complete or refused).
    The gate copies a `running` record in too, a box's progress list among them, and that is no answer."""
    if path is None:
        return False
    try:
        record = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    return isinstance(record, dict) and record.get("state") in ("complete", "refused")


_REMOTE_STATE = {"passed": "passed", "red": "RED", "FAILED": "RED", "did not reproduce": "passed",
                 "UNMEASURED": "pending"}


def _live_remote(box: str, live: dict | None) -> dict:
    """Extra fields for a running remote slice's step: the box's own lane rows and a one-line summary."""
    if not live:
        return {}
    if live.get("problem"):
        return {"live_problem": live["problem"]}
    rows = [dict(r, lane=f"{box}:{r['lane']}") for r in live.get("rows", [])]
    finished = [r for r in rows if r["state"] in ("passed", "RED")]
    reds = [r for r in finished if r["state"] == "RED"]
    parts = [f"{len(finished)}/{len(rows)} suites finished" + (f", {len(reds)} red" if reds else "")]
    parts += [f"lane {d['lane']}: {d['suite']} {d['elapsed']}" + (f" {d['progress']}" if d.get("progress", "").endswith("%") else "")
              for d in live.get("now", []) if d.get("lane")]
    return {"live_rows": rows, "live_text": "; ".join(parts)[:400]}


def _report_suites(phase: str, blocks: Sequence[dict]) -> list[dict]:
    """A pass's suites as the gate's own report printed them, one row each, lane by lane."""
    out = []
    for block in blocks:
        lane = int(block["lane"]) if str(block["lane"]).isdigit() else block["lane"]
        for hit in block["suites"]:
            dur = sv._DURATION.search(hit["text"])
            out.append({"phase": phase, "lane": lane, "suite": hit["suite"].removeprefix("tests/"),
                        "state": _REMOTE_STATE.get(hit["state"], "pending"),
                        "took": sv.fmt_seconds(float(dur.group(1))) if dur else "", "result": hit["text"],
                        "note": hit["state"] if hit["state"] == "did not reproduce" else ""})
    return out


def _suites_of(step: dict, rows: list[dict]) -> list[dict]:
    """The suite rows that belong inside a pass's step: this pass's lanes, one re-run's suite, or a remote slice's."""
    name = step["step"]
    if step.get("listed"):
        return step["listed"]
    if step.get("live_rows"):
        return step["live_rows"]
    if step.get("raw") is not None:
        out = []
        for hit in step["raw"]:
            dur = sv._DURATION.search(hit["text"])
            out.append({"phase": "remote", "lane": step.get("box", ""), "suite": hit["suite"].removeprefix("tests/"),
                        "state": _REMOTE_STATE.get(hit["state"], "pending"),
                        "took": sv.fmt_seconds(float(dur.group(1))) if dur else "", "result": hit["text"],
                        "note": hit["state"] if hit["state"] == "did not reproduce" else ""})
        return out
    if name.startswith("wave pass"):
        return [r for r in rows if r["phase"] == "wave"]
    if name.startswith("restart wave"):
        return [r for r in rows if r["phase"] == "restarts"]
    if name.startswith("alone phase"):
        return [r for r in rows if r["phase"] == "alone"]
    if name.startswith("re-run alone: "):
        short = name.split(": ", 1)[1]
        return [r for r in rows if r["phase"] == "rerun" and r["suite"].rsplit("/", 1)[-1] == short]
    return []


def _live_detail(gate: dict | None, phase: str, lane: int) -> str:
    """What a running pass is doing: suites finished of planned, reds, and each lane's current suite."""
    rows = [r for r in (gate or {}).get("rows", []) if r["phase"] == phase]
    if not rows:
        return f"running on lane {lane}"
    finished = [r for r in rows if r["state"] in ("passed", "RED")]
    reds = [r for r in finished if r["state"] == "RED"]
    parts = [f"{len(finished)}/{len(rows)} suites finished" + (f", {len(reds)} red" if reds else "")]
    for doing in (gate or {}).get("now", []):
        if doing["phase"] != phase or not doing.get("lane"):
            continue
        if "(between" in doing["suite"]:
            parts.append(f"lane {doing['lane']}: stack bring-up or teardown"
                         + (f" ({doing['last_line'][:50]})" if doing.get("last_line") else ""))
        else:
            parts.append(f"lane {doing['lane']}: {doing['suite']} {doing['elapsed']}"
                         + (f" {doing['progress']}" if doing.get("progress", "").endswith("%") else ""))
    return "; ".join(parts)[:400]


def build_steps(repo: Path, procs: list[dict], gate: dict | None, batch: dict, gate_log: Path | GateLogRef | None,
                memory: StepMemory | None, waiting: Sequence[str], now: float,
                box_status: Mapping[str, dict] | None = None,
                box_live: Mapping[str, dict] | None = None, remote_box: str = "",
                built_in: Path | None = None) -> list[dict]:
    """The integration timeline: what is done with its times, what runs now, what comes next.

    `built_in` is the tree the batch is merged and fast-tested in, when it is not `repo`.

    `remote_box` names the box the gate runs on when it is not this one: then no file beside its log (slice
    declarations, evidence, lane logs) is looked for here, where a same-named path would be another run's.
    """
    gate_log = sv._as_ref(gate_log)
    local_log = gate_log is not None and not gate_log.box
    steps: list[dict] = []
    merging = sv.current_merge(procs)
    if batch.get("ledger"):
        steps.append({"step": f"{batch['ledger'].get('name') or 'Batch' + str(batch['ledger']['number'])} ledger "
                              "(the previous batch landed)", "detail": "",
                      "state": "done", "start": None, "end": batch["ledger"]["ts"]})
    for merge in batch["merges"]:
        began = memory.started_of(merge["seam"]) if memory else None
        steps.append({"step": f"merge {merge['seam']}", "detail": merge["sha"], "state": "done",
                      "start": began, "end": merge["at"]})
    ref = sv.trunk_ref(repo)
    for rev in batch["revisions"]:
        got = sv.sent_back_status(repo, ref, rev["subject"], rev["at"])
        steps.append({"step": f"sent back to its owner: {got['seam']}", "detail": got["text"],
                      "state": {"landed": "done", "reworked": "reworked"}.get(got["status"], "sent-back"),
                      "start": None, "end": rev["at"]})
    if merging:
        steps.append({"step": "merging now", "detail": merging, "state": "RUNNING",
                      "start": memory.started_of(merging) if memory else None, "end": None})
    elif sv.merge_in_progress(repo):
        steps.append({"step": "a git merge is open (MERGE_HEAD)", "detail": "", "state": "RUNNING",
                      "start": None, "end": None})
    firsts = [m["at"] for m in batch["merges"]]
    steps += _unit_runs(built_in or repo, procs, min(firsts) if firsts else None)
    parsed = sv.parse_gate_log(gate_log.read()) if gate_log else {"plan": [], "blocks": [], "verdict": [], "boxes": {},
                                                               "reruns": [], "label": ""}
    started = gate_log.started if gate_log else None
    art = (gate_log.art_dir, gate_log.art_stem) if local_log else \
        None if gate_log is not None or remote_box else _tree_artifacts(procs)
    if not parsed["label"] and gate and gate.get("state") == "running":
        # gate_run prints its plan when the run ends, so mid-run only the process, the lane scripts and the
        # slice files tell what is under way.
        started = gate.get("started_ts")
        parsed["label"] = gate["label"]
        parsed["boxes"] = _slice_boxes(art)
        steps.append({"step": f"gate {gate['label']} started", "state": "done", "start": None, "end": started,
                      "detail": "gate_run prints its plan only when the run ends: the passes shown are those seen running; "
                                "later passes appear when they start"})
        parsed["live_unplanned"] = True
    if parsed["label"] and not parsed.get("live_unplanned"):
        steps.append({"step": f"gate {parsed['label']} started", "detail": "; ".join(parsed["plan"][4:6])[:260],
                      "state": "done", "start": None, "end": started})
    if not parsed["label"] and gate and gate.get("state") == "finished":
        # A finished gate whose report is not to be had: its lane logs still say it ran and when it ended.
        where = f" on {gate['box']}" if gate.get("box") else ""
        steps.append({"step": f"gate {gate['label']} finished", "state": "done", "start": None,
                      "end": sv._num(gate.get("ended_at")),
                      "detail": f"its report was not found{where}: its passes are read from its lane logs"})
    live_passes = (gate or {}).get("live_passes", [])
    groups: dict[str, list[dict]] = {}
    for block in parsed["blocks"]:
        if block["remote"]:
            box = block["lane"]
            dec = art[0] / f"{art[1]}-declaration-{box}.json" if local_log and art else None
            ev = art[0] / f"{art[1]}-evidence-{box}.json" if local_log and art else None
            end = ev.stat().st_mtime if ev is not None and ev.exists() else None
            begin = dec.stat().st_mtime if dec is not None and dec.exists() else None
            reds = [x for x in block["suites"] if x["state"] in ("red", "FAILED")]
            steps.append({"step": f"remote slice on {box}", "detail": f"{len(block['suites'])} suite(s), "
                          f"{block['wall']}s" + (f", {len(reds)} red: " + ", ".join(x['suite'].rsplit('/', 1)[-1] for x in reds)
                                                 if reds else ""),
                          "state": "done" if block["exit"] == 0 else "RED", "start": begin, "end": end,
                          "raw": block["suites"], "box": box})
            continue
        try:
            # A lane log another box names is that box's file: a same-named one here is another run's.
            end = Path(block["log"]).stat().st_mtime if local_log else None
        except OSError:
            end = None
        label_m = sv._LANE_LOG.match(Path(block["log"]).name)
        phase = sv._PHASE_SUFFIX.get(label_m.group(3) or "", "rerun") if label_m else "wave"
        if phase == "wave" and label_m and label_m.group(2).endswith("-clamd"):
            phase = "clamd"     # the clamd wave's own lane log, never a part of the wave
        if phase == "rerun":
            outcome = block["suites"][0] if block["suites"] else {"state": "?", "suite": "?", "text": ""}
            steps.append({"step": f"re-run alone: {outcome['suite'].rsplit('/', 1)[-1]}",
                          "detail": f"{outcome['state']}: {outcome['text']}"[:200],
                          "state": "done" if block["exit"] == 0 else "RED",
                          "start": end - block["wall"] if end else None, "end": end})
        else:
            groups.setdefault(phase, []).append({**block, "end": end})
    titles = {"wave": "wave pass (main suites, parallel lanes)", "restarts": "restart wave",
              "clamd": "clamd wave (the real malware engine, one lane)",
              "alone": "alone phase (suites needing the box to themselves)"}
    for phase in ("wave", "restarts", "clamd", "alone"):
        blocks = groups.get(phase)
        if not blocks:
            continue
        suites = [x for b in blocks for x in b["suites"]]
        reds = [x for x in suites if x["state"] in ("red", "FAILED")]
        ends = [b["end"] for b in blocks if b["end"]]
        # The wave's lane log keeps being appended to by later passes, so its mtime is no end: the wave began
        # with the gate and lasted as long as its longest lane. The later passes own their own log files.
        wave_end = (started + max(b["wall"] for b in blocks)) if phase == "wave" and started else None
        # Every suite of the pass is listed inside its step: from this gate's lane rows (with their usual
        # times) when this page reads them, else from the lines of the report itself, so a gate read from
        # another box's report or from lane logs this page cannot open still shows each suite.
        own = [r for r in (gate or {}).get("rows", []) if r["phase"] == phase]
        listed = None if own and (gate or {}).get("label") == parsed["label"] else _report_suites(phase, blocks)
        if phase == "clamd" and not listed:
            listed = _report_suites(phase, blocks)  # the lane rows file the clamd lane under the wave
        steps.append({"step": titles[phase], **({"listed": listed} if listed else {}),
                      "detail": f"{len(blocks)} lane(s), {len(suites)} suite(s)"
                      + (f", {len(reds)} red: " + ", ".join(x["suite"].rsplit("/", 1)[-1] for x in reds) if reds else ""),
                      "state": "done" if all(b["exit"] == 0 for b in blocks) else "RED",
                      "start": started if phase == "wave" and started else
                      min((b["end"] - b["wall"] for b in blocks if b["end"]), default=None),
                      "end": wave_end if phase == "wave" else (max(ends) if ends else None)})
    # Passes that finished but are in no master log yet (gate_run prints it when the run ends): read from lane logs.
    live_phases = {lp["phase"] for lp in live_passes}
    for phase in ("wave", "restarts", "alone"):
        mine = [r for r in (gate or {}).get("rows", []) if r["phase"] == phase]
        if phase in groups or phase in live_phases or not mine:
            continue
        if any(r["state"] in ("RUNNING", "pending") for r in mine):
            continue
        reds = [r for r in mine if r["state"] == "RED"]
        ends = [r["log_end"] for r in mine if r.get("log_end")]
        steps.append({"step": titles[phase], "state": "RED" if reds else "done",
                      "detail": f"{len({r['lane'] for r in mine})} lane(s), {len(mine)} suite(s)"
                                + (f", {len(reds)} red: " + ", ".join(r["suite"].rsplit("/", 1)[-1] for r in reds) if reds else "")
                                + " (from the lane logs; the gate's own log is not printed until the run ends)",
                      "start": started if phase == "wave" else None,
                      "end": (started + 0 if False else None) or (max(ends) if ends else None)})
    # running now
    seen_running = set()
    for lp in live_passes:
        key = (lp["phase"], lp["label"] if lp["phase"] == "rerun" else "")
        if key in seen_running:
            continue
        seen_running.add(key)
        steps.append({"step": {"wave": "wave pass (main suites, parallel lanes)", "restarts": "restart wave",
                               "alone": "alone phase (suites needing the box to themselves)",
                               "rerun": "re-run alone: " + lp["label"].split("-rerun-", 1)[-1]}[lp["phase"]],
                      "detail": _live_detail(gate, lp["phase"], lp["lane"]), "state": "RUNNING",
                      "start": lp["started"], "end": None})
    for box, count in parsed["boxes"].items():
        if any(b["remote"] and b["lane"] == box for b in parsed["blocks"]):
            continue
        dec = art[0] / f"{art[1]}-declaration-{box}.json" if art else None
        ev = art[0] / f"{art[1]}-evidence-{box}.json" if art else None
        up = bool(dec and dec.exists())
        # The gate copies every record the box publishes into this file, a `running` one too: only a final
        # record in it is the box's evidence back.
        back = _final_evidence(ev)
        mine = (box_status or {}).get(box) or {}
        if mine.get("state") == "complete" and mine.get("suites") and not back:
            # The box has published its result on its status ref before the gate collected it.
            reds = [x for x in mine["suites"] if x.get("rc") != 0]
            steps.append({"step": f"remote slice on {box}",
                          "detail": f"{len(mine['suites'])} suite(s), {mine.get('wall_seconds', 0):.0f}s"
                                    + (f", {len(reds)} red: " + ", ".join(x['suite'].rsplit('/', 1)[-1] for x in reds) if reds else "")
                                    + ". box says complete (result read from its status ref)",
                          "state": "RED" if reds or mine.get("gate_exit") else "done",
                          "start": mine.get("started_at"), "end": mine.get("finished_at"), "box": box,
                          "raw": [{"state": "passed" if x.get("rc") == 0 else "red", "suite": x["suite"],
                                   "text": f"{x['summary']}" or f"rc={x.get('rc')}"} for x in mine["suites"]]})
            continue
        partial = mine.get("suites_done") if mine.get("state") == "running" and not back else None
        steps.append({"step": f"remote slice on {box}",
                      "detail": (f"{count} suite(s)" + (": evidence returned" if back else "") + ". "
                                 + sv.box_status_text((box_status or {}).get(box), now)).strip(),
                      "state": "done" if back else ("RUNNING" if up or partial is not None else "next"),
                      "start": dec.stat().st_mtime if up else None, "end": ev.stat().st_mtime if back else None,
                      "raw": sv._partial_suites(art, box, partial) if partial is not None else _slice_suites(art, box),
                      "box": box, **_live_remote(box, (box_live or {}).get(box))})
    # coming up, from the plan
    done_phases = set(groups) | {k[0] for k in seen_running}
    plan_text = " ".join(parsed["plan"])
    if parsed["label"] and not parsed["verdict"]:
        if "restart wave" in plan_text and "restarts" not in done_phases:
            steps.append({"step": "restart wave", "detail": "planned", "state": "next", "start": None, "end": None})
        if "alone phase" in plan_text and "alone" not in done_phases:
            steps.append({"step": "alone phase (suites needing the box to themselves)", "detail": "planned",
                          "state": "next", "start": None, "end": None})
        rerun_done = {s["step"].split(": ", 1)[-1] for s in steps if s["step"].startswith("re-run alone")}
        for plan in parsed["reruns"]:
            for suite in plan["suites"]:
                short = suite.rsplit("/", 1)[-1]
                if short not in rerun_done and not any(s["step"] == "re-run alone: " + short for s in steps):
                    steps.append({"step": "re-run alone: " + short, "detail": f"planned, {plan['when']}",
                                  "state": "next", "start": None, "end": None})
    if parsed["verdict"]:
        verdict = next((v for v in parsed["verdict"] if sv._VERDICT.match(v)), parsed["verdict"][0])
        steps.append({"step": f"gate verdict: {verdict}", "detail": " | ".join(
            v for v in parsed["verdict"] if v != verdict)[:300], "state": "RED" if "RED" in verdict else "done",
            "start": None, "end": gate_log.mtime() if gate_log else None})
    elif not parsed["label"] and not batch.get("landed") and not (gate and gate.get("state") in ("running", "finished")):
        steps.append({"step": "distributed gate", "detail": "not started", "state": "next", "start": None, "end": None})
    if batch.get("landed"):
        steps.append({"step": f"Batch{batch['landed']['number']} ledger commit", "detail": batch["landed"]["subject"],
                      "state": "done", "start": None, "end": batch["landed"]["ts"]})
    else:
        # The seams pushed and not merged (`waiting`) are the board, not this batch: they were one unexplained
        # "N other pushed seams on the board" step here (owner, 2026-10-07), and are now a note of the section
        # (`board_note`), never a step of the run.
        steps.append({"step": "ledger commit (row deletions) and push", "detail": "after the gate is green",
                      "state": "next", "start": None, "end": None})
    def order(item: tuple[int, dict]) -> tuple:
        idx, st = item
        if st["state"] == "next":
            return (2, idx, 0.0)
        if st["state"] == "RUNNING":
            return (1, idx, st["start"] or now)
        return (0, idx, st["end"] or st["start"] or 0.0)

    steps = [st for _, st in sorted(enumerate(steps), key=lambda it: (order(it)[0], order(it)[2], it[0]))]
    out = []
    latest_end = None
    for n, st in enumerate(steps, 1):
        row = {"n": n, "step": st["step"], "detail": st["detail"], "state": st["state"],
               "started": sv._stamp(st["start"], now), "finished": sv._stamp(st["end"], now),
               # The epoch times as well, for the run plan's elapsed time and ETA.
               "start_ts": st["start"], "end_ts": st["end"]}
        if st.get("live_text"):
            row["detail"] = (row["detail"] + " Live over ssh: " + st["live_text"]).strip()
        elif st.get("live_problem"):
            row["detail"] = (row["detail"] + " Live probe: " + st["live_problem"]).strip()
        inside = _suites_of(st, (gate or {}).get("rows", []))
        if inside:
            row["suites"] = inside
        for key in ("run", "failures", "failures_total"):
            if key in st:
                row[key] = st[key]
        means = step_meaning(st["step"])
        if means:
            row["what"] = means
        begin = st["start"] or (st["end"] if st["state"] not in ("next", "RUNNING") else None)
        if latest_end is not None and begin is not None and begin - latest_end >= sv.GAP_SECONDS:
            row["gap_before"] = sv.fmt_seconds(begin - latest_end)
        if st["end"] and (latest_end is None or st["end"] > latest_end):
            latest_end = st["end"]
        if st["start"] and st["end"]:
            row["took"] = sv.fmt_seconds(st["end"] - st["start"])
        elif st["start"] and st["state"] == "RUNNING":
            row["took"] = sv.fmt_seconds(now - st["start"]) + " so far"
        out.append(row)
    return out
