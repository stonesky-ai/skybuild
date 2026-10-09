"""The batch: trunk's merges since the last ledger, the unit lane, the merge in progress, the stage, and
the Integrator-now section that gathers them.
"""
from __future__ import annotations

import os
import re
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import hub as sv
from .processes import run_text

if TYPE_CHECKING:      # names used in annotations only (strings at runtime)
    from .fleet import RemoteProbe
    from .processes import Runner
    from .steps import StepMemory


# ---------------------------------------------------------------- batch, seams, unit lane

#: Names the trunk when the checkout cannot: a `git for-each-ref` pattern, whose newest match by version is
#: the trunk (the todo service finds its trunk the same way). No default (ADR-0002).
TRUNK_REFS_VAR = "SESSIONVIEW_WEB_TRUNK_REFS"
TRUNK_IS_HEAD = (f"the trunk shown is this checkout's own HEAD, which may be far behind the pushed trunk: its branch "
                 f"tracks no upstream and {TRUNK_REFS_VAR} is unset")


def trunk_ref(repo: Path, runner: Runner = run_text) -> str:
    """The pushed trunk, never this checkout's own HEAD while anything better is known: a checkout can sit
    hundreds of commits behind the tree the integrator really merges on, and a timeline read from it would be
    of the wrong batch.

    The setting's pattern first, then the upstream this checkout tracks. On 2026-10-06 the checkout sat on a
    branch whose upstream had been deleted at a branch boundary, so the page read its HEAD and showed Batch71
    as the last ledger while the pushed trunk was at Batch79.
    """
    pattern = os.environ.get(TRUNK_REFS_VAR, "").strip()
    if pattern:
        listed = runner(["git", "-C", str(repo), "for-each-ref", "--sort=version:refname",
                         "--format=%(refname:short)", pattern]).split()
        if listed and re.fullmatch(r"[\w./-]+", listed[-1]):
            return listed[-1]
    named = runner(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "@{upstream}"]).strip()
    return named if re.fullmatch(r"[\w./-]+", named) else "HEAD"


def sent_back_status(repo: Path, ref: str, subject: str, at: float, runner: Runner = run_text) -> dict:
    """What became of a seam the integrator sent back: landed in a ledger, re-pushed, or still waiting."""
    seam = subject.split(":", 1)[0].strip()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9-]*", seam):
        return {"seam": seam, "status": "unknown", "text": "the seam id could not be read from the commit"}
    ledger = runner(["git", "-C", str(repo), "log", ref, "--first-parent", "--extended-regexp", "--all-match",
                     "--grep=^Batch[0-9]+ ledger", f"--grep={seam}", "--format=%ct\t%s", "-3"]).splitlines()
    for line in ledger:
        stamp, _, text = line.partition("\t")
        if stamp.isdigit() and float(stamp) > at:
            hit = sv._LEDGER.match(text)
            return {"seam": seam, "status": "landed", "text": f"landed in Batch{hit.group(1) if hit else '?'} at {sv._stamp(float(stamp))}"}
    tips = runner(["git", "-C", str(repo), "for-each-ref", "--sort=-committerdate",
                   "--format=%(committerdate:unix) %(refname:short)", f"refs/remotes/origin/seam/{seam}*"]).splitlines()
    for line in tips:
        stamp, _, name = line.partition(" ")
        if stamp.isdigit() and float(stamp) > at:
            who = runner(["git", "-C", str(repo), "log", "-1", "--format=%(trailers:key=Agent,valueonly)", name]).strip()
            return {"seam": seam, "status": "reworked",
                    "text": f"a new tip was pushed at {sv._stamp(float(stamp))} ({name.removeprefix('origin/')})"
                            + (f" by {who}" if who else "") + ": it waits for the integrator to merge it"}
    return {"seam": seam, "status": "waiting", "text": "no new tip pushed and not landed since: whoever holds the row "
            "(a stream or agent that claimed it) has not delivered, or nobody has claimed it"}


def trunk_segments(repo: Path, runner: Runner = run_text) -> tuple[list[dict], set[str]]:
    """The pushed trunk's first-parent history cut at its ledger commits, newest first, and every seam merged.

    `segments[i]["ledger"]` is the ledger commit that bounds segment i below: segment 0 holds what is newer
    than the newest ledger commit, segment 1 the batch that commit landed.
    """
    ref = trunk_ref(repo, runner)
    out = runner(["git", "-C", str(repo), "log", ref, "--first-parent", "--format=%ct\t%h\t%s", "-120"])
    segments: list[dict] = [{"ledger": None, "merges": [], "revisions": []}]
    merged_all: set[str] = set()
    for line in out.splitlines():
        stamp, _, rest = line.partition("\t")
        sha, _, subject = rest.partition("\t")
        if not stamp.isdigit():
            continue
        hit = sv._LEDGER.match(subject)
        if hit or sv._TOOLING_LEDGER.match(subject):
            # A tooling batch's ledger has no number: it bounds a batch all the same.
            segments[-1]["ledger"] = {"number": int(hit.group(1)) if hit else None,
                                      "name": f"Batch{hit.group(1)}" if hit else "the tooling batch",
                                      "sha": sha, "ts": float(stamp),
                                      "at": sv._clock(float(stamp)), "subject": subject[:140]}
            segments.append({"ledger": None, "merges": [], "revisions": []})
            continue
        merged = sv._MERGE.match(subject)
        if merged:
            merged_all.add(merged.group(1))
            segments[-1]["merges"].append({"seam": merged.group(1), "sha": sha, "at": float(stamp)})
        elif sv._REVISION.search(subject):
            segments[-1]["revisions"].append({"sha": sha, "at": float(stamp), "subject": subject[:140]})
    return segments, merged_all


def batch_seams(repo: Path, runner: Runner = run_text, gate_started: float | None = None,
                trunk: tuple[list[dict], set[str]] | None = None) -> dict:
    """The seams merged in the batch, oldest first, and the revision (sent-back) commits among them.

    The batch is what lies since the newest ledger commit — unless the running or
    last gate began BEFORE that ledger commit, which then landed this batch: the
    batch is the span before it, and `landed` carries that ledger commit. `trunk`
    is `trunk_segments`' answer when the caller has read it already.
    """
    segments, merged_all = trunk if trunk is not None else trunk_segments(repo, runner)
    newest = segments[0]["ledger"]
    # A tooling batch lands with no gate, and may land while a gate runs: its ledger never landed the gated batch.
    if gate_started is not None and newest and newest["number"] is not None and newest["ts"] > gate_started \
            and len(segments) > 1:
        chosen, landed, previous = segments[1], newest, segments[1]["ledger"]
    else:
        chosen, landed, previous = segments[0], None, newest
    return {"ledger": previous, "landed": landed, "merged_all": merged_all,
            "merges": list(reversed(chosen["merges"])), "revisions": list(reversed(chosen["revisions"]))}


#: The programs whose working directory may be the tree a batch is being built in.
_TREE_PROGRAMS = ("gate_run.py", "seam_merge.py", "seam_land.py")


def tree_merges(tree: Path, runner: Runner = run_text) -> tuple[str, list[dict]]:
    """The branch of `tree` and the seams merged on it that the trunk it tracks does not have, oldest first.

    Read from the tree's own first-parent line past its upstream, so it is right whatever this page's
    checkout knows. A seam's own worktree is never a batch tree, a bundle seam's merges included; a
    directory that is no checkout, or a branch that tracks nothing, has no merges.
    """
    branch = runner(["git", "-C", str(tree), "rev-parse", "--abbrev-ref", "HEAD"]).strip()
    if not branch or branch.startswith(("seam/", "worktree-agent-")):
        return branch, []
    out = runner(["git", "-C", str(tree), "log", "--first-parent", "--format=%ct\t%h\t%s", "-200", "@{upstream}..HEAD"])
    merges = []
    for line in out.splitlines():
        stamp, _, rest = line.partition("\t")
        sha, _, subject = rest.partition("\t")
        merged = sv._MERGE.match(subject) if stamp.isdigit() else None
        if merged:
            merges.append({"seam": merged.group(1), "sha": sha, "at": float(stamp)})
    merges.reverse()
    return branch, merges


def batch_tree(repo: Path, procs: list[dict], runner: Runner = run_text,
               memory: StepMemory | None = None) -> dict | None:
    """The checkout a batch is being built in when it is not `repo`: {path, branch, merges}, or None.

    The integrator merges a batch in a tree of its own and pushes only with the ledger commit, so until
    then the pushed trunk shows no merge at all. The tree is found by what runs in it (the gate, a merge,
    the fast tests), by the scratch setting's checkouts, or because it was found a moment ago and still
    holds merges the trunk does not have: between two of the integrator's commands nothing runs in it.
    """
    try:
        own = repo.resolve()
    except OSError:
        own = repo
    found: list[Path] = []
    for proc in procs:
        argv = sv._argv(proc["args"])
        if any(a.rsplit("/", 1)[-1] in _TREE_PROGRAMS for a in argv) \
                or ("skykeep.sh" in proc["args"] and " testfast" in proc["args"]):
            cwd = sv._cwd_of(proc["pid"])
            if cwd:
                found.append(Path(cwd))
    if memory is not None and memory.batch_tree:
        found.append(Path(memory.batch_tree))
    found += sv._scratch_roots()
    seen: set[Path] = set()
    for tree in found:
        try:
            tree = tree.resolve()
        except OSError:
            continue
        if tree == own or tree in seen:
            continue
        seen.add(tree)
        branch, merges = tree_merges(tree, runner)
        if merges:
            if memory is not None:
                memory.batch_tree = str(tree)
            return {"path": tree, "branch": branch, "merges": merges}
    if memory is not None:
        memory.batch_tree = ""
    return None


def merge_in_progress(repo: Path) -> bool:
    return (repo / ".git" / "MERGE_HEAD").exists()


def _checkout_testfast(repo: Path, procs: list[dict]) -> list[dict]:
    """The `skykeep.sh testfast` runs in THIS checkout (skykeep.sh cds to its own tree's root).

    A seam agent's testfast in a worktree is not the integrator's unit lane, and a cwd that
    cannot be read is not this checkout.
    """
    try:
        root = repo.resolve()
    except OSError:
        return []
    out = []
    for proc in procs:
        if "skykeep.sh" not in proc["args"] or " testfast" not in proc["args"]:
            continue
        cwd = sv._cwd_of(proc["pid"])
        try:
            if cwd and Path(cwd).resolve() == root:
                out.append(proc)
        except OSError:
            continue
    return out


def unit_lane(repo: Path, procs: list[dict], since: float | None) -> dict:
    """The newest fast-test run's log (`skykeep.sh testfast`, the unit lane): running, or its verdict and duration."""
    running = _checkout_testfast(repo, procs)
    logs = []
    try:
        logs = sorted((repo / "test-logs").glob("testfast-unit-*.log"), key=lambda p: p.stat().st_mtime)
    except OSError:
        pass
    newest = logs[-1] if logs else None
    out: dict[str, Any] = {"state": "running" if running else "not running"}
    if running:
        out["elapsed"] = sv.fmt_seconds(min(p["elapsed"] for p in running))
    if newest is not None:
        tail = sv._read(newest)[-3000:]
        hit = None
        for hit in sv._UNIT_RESULT.finditer(tail):
            pass
        failed = sv._UNIT_FAILED.search(tail)
        out["last"] = {"file": newest.name, "finished": sv._clock(newest.stat().st_mtime),
                       "result": f"{hit.group(1)} passed" + (f", {failed.group(1)} failed" if failed else ", 0 failed")
                       if hit else "no verdict line (cut off or still running)",
                       "took": sv.fmt_seconds(float(hit.group(3))) if hit else "unknown",
                       "after_the_merges": bool(since is None or newest.stat().st_mtime >= since)}
    return out


def current_merge(procs: list[dict]) -> str | None:
    """The running `seam_merge.py` / `seam_land.py`, judged by the program a process runs, never by a word in its args."""
    for proc in procs:
        argv = sv._argv(proc["args"])
        program = argv[1] if argv and Path(argv[0]).name.startswith(("python", "uv")) and len(argv) > 1 else (argv[0] if argv else "")
        if Path(program).name in ("seam_merge.py", "seam_land.py"):
            return " ".join(argv[-6:])[:160]
    return None


# ---------------------------------------------------------------- gates on other boxes

#: The boxes a whole gate (or a slice of one) may run on, read over the ssh probe. No default (ADR-0002).
GATE_BOXES_VAR = "SESSIONVIEW_WEB_GATE_REMOTE_BOXES"
#: Absolute glob(s), `os.pathsep`-separated, of the files on a gate box that may hold a gate's console report
#: (gate_run's stdout, or a tree's `test-logs/gates-*.log`). Sent to the box ahead of its probe. No default.
GATE_REPORTS_VAR = "SESSIONVIEW_WEB_GATE_REMOTE_REPORTS"
GATE_BOXES_UNSET = f"{GATE_BOXES_VAR} unset: only this box's gates are read"
#: What `RemoteProbe.get` says while a box's first probe is still out: not yet an answer, so no warning.
PROBE_PENDING = "first ssh probe still running"
#: Report files a box looks through, newest first; bytes read of each; bytes of report text it sends back.
REPORT_FILES_SCANNED = 12
REPORT_READ_CAP = 2_000_000
REPORT_SEND_CAP = 65536

_REPORT_WRITTEN = re.compile(r"^report written to (\S+)\s*$", re.MULTILINE)
# Every list a gate section carries, with the keys each of its items must have: a box's answer that lacks
# one is "not readable", never a KeyError inside a page refresh.
_GATE_ITEMS = {"rows": ("phase", "state", "suite", "lane"), "now": ("phase", "suite", "elapsed"),
               "live_passes": ("phase", "lane", "label", "started"), "warnings": ("code", "subject", "text")}


def gate_remote_boxes() -> list[str]:
    """The boxes `SESSIONVIEW_WEB_GATE_REMOTE_BOXES` names (commas or spaces); unset or empty reads this box only."""
    return os.environ.get(GATE_BOXES_VAR, "").replace(",", " ").split()


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _gate_readable(gate: Any) -> bool:
    """A gate section as `gate_section` makes it: what a box sends is checked before the page leans on it."""
    if not isinstance(gate, dict) or not isinstance(gate.get("label"), str) \
            or gate.get("state") not in ("running", "finished"):
        return False
    for key, needs in _GATE_ITEMS.items():
        items = gate.get(key, [])
        if not isinstance(items, list) or not all(isinstance(x, dict) and all(k in x for k in needs) for x in items):
            return False
    if any(lp["phase"] not in sv.PHASES for lp in gate.get("live_passes", [])):
        return False
    if not all(isinstance(gate.get(k, []), list) for k in ("notes", "rework")):
        return False
    return all(gate.get(k) is None or _num(gate.get(k)) is not None for k in ("started_at", "ended_at", "started_ts"))


def read_gate_boxes(remote: RemoteProbe | None, boxes: Sequence[str], now: float) -> list[dict]:
    """Each gate box's newest gate and report as its probe last sent them, or why the box was not read.

    Never waits: the probe answers from its cache and refreshes in the background.
    """
    out: list[dict] = []
    for box in boxes:
        got = remote.get(box, now) if remote is not None else {"problem": "no ssh probe is wired to this view"}
        data = got.get("data") if isinstance(got.get("data"), dict) else None
        if data is None or "gate" not in data:
            out.append({"box": box, "problem": sv._redact(str(got.get("problem") or "the box's probe sent no gate data"))})
            continue
        gate = data.get("gate")
        if gate is not None and not _gate_readable(gate):
            out.append({"box": box, "problem": "the box's gate data is not readable"})
            continue
        report = data.get("gate_report")
        if not (isinstance(report, dict) and isinstance(report.get("text"), str) and isinstance(report.get("path"), str)):
            report = None
        out.append({"box": box, "gate": gate, "report": report, "at": _num(got.get("at"))})
    return out


def _gate_rank(gate: Mapping[str, Any] | None) -> tuple[int, float]:
    """A running gate first, then a running slice of some box's gate, then a finished one; newest start first."""
    if not gate:
        return (-1, 0.0)
    running = gate.get("state") == "running"
    return (2 if running and not gate.get("slice") else 1 if running else 0, _num(gate.get("started_at")) or 0.0)


def pick_gate(local: dict | None, here: str, boxes: Sequence[Mapping[str, Any]],
              prefer: int | None = None) -> tuple[str, dict | None, dict | None]:
    """(box, gate, report) of the newest gate on this box or any gate box; this box wins a tie.

    `prefer` is the batch the integrator's own log says it is on: a gate of that batch wins over a newer one
    of anything else (a keeper's evidence run on a gate box is not the batch).
    """
    best: tuple[str, dict | None, dict | None] = (here, local, None)
    for entry in boxes:
        gate = entry.get("gate")
        if gate and _gate_rank(gate) > _gate_rank(best[1]):
            best = (str(entry["box"]), gate, entry.get("report"))
    if prefer is not None:
        every = [(here, local, None)] + [(str(e["box"]), e.get("gate"), e.get("report")) for e in boxes]
        mine = [one for one in every if one[1] and (hit := sv._BATCH_OF_LABEL.match(str(one[1].get("label", ""))))
                and int(hit.group(1)) == prefer]
        if mine:
            return max(mine, key=lambda one: _gate_rank(one[1]))
    return best


def _remote_top(box: str, gate: dict, at: float | None, now: float) -> dict:
    """A box's gate as this page shows it: its times in this page's zone and age, its warnings named by box."""
    started = _num(gate.get("started_at"))
    running = gate["state"] == "running"
    read = f"this gate runs on {box}: read over ssh" + (f" {sv.fmt_seconds(now - at)} ago" if at else "")
    return dict(gate, box=box, started=sv._clock(started) if started else str(gate.get("started", "unknown")),
                running_for=sv.fmt_seconds(now - started) if running and started else str(gate.get("running_for", "-")),
                notes=[*gate.get("notes", []), read],
                warnings=[dict(w, subject=f"{box}:{w['subject']}", text=f"{box}: {w['text']}")
                          for w in gate.get("warnings", [])])


def _gate_box_text(box: str, entry: Mapping[str, Any]) -> str:
    """One box's line under the panel's top: its newest gate, or why it was not read."""
    problem = entry.get("problem")
    if problem:
        return f"{box}: not read yet ({problem})" if problem.startswith(PROBE_PENDING) else f"{box}: no answer ({problem})"
    gate = entry.get("gate")
    if not gate:
        return f"{box}: no gate on record"
    return f"{box}: {gate['label']} {gate['state']}" + (" (a slice of another box's gate)" if gate.get("slice") else "")


def stale_gate(gate: Mapping[str, Any] | None, where: str, started: float | None, segments: Sequence[dict]) -> str:
    """What to say instead of the top, when a FINISHED top gate is older than the ledger; "" when it is current.

    The ledger commit after a gate is the one that landed it; a second batch number landed after it means
    a batch landed whose gate this page never saw (it ran on a box nobody named, or ran nowhere). A running
    gate is what happens now, whatever the ledger says.
    """
    if not gate or gate.get("state") != "finished" or started is None:
        return ""
    # Numbered ledgers only: a tooling batch lands with no gate, so its ledger says nothing about this one.
    after = [seg["ledger"] for seg in segments if seg.get("ledger") and seg["ledger"]["ts"] > started
             and seg["ledger"]["number"] is not None]
    if len({ledger["number"] for ledger in after}) < 2:
        return ""
    return (f"last gate seen: {gate['label']} on {where}; newer ledger: Batch{after[0]['number']} "
            "landed without a gate seen here")


# ---------------------------------------------------------------- the section

def stage_of(gate: dict | None, unit: dict, merging: str | None, in_merge: bool) -> str:
    if merging or in_merge:
        return "merging seams"
    if unit.get("state") == "running":
        return sv.FAST_TESTS
    if gate is not None and gate.get("state") == "running":
        return "distributed gate"
    return "between batches: no merge, fast-test run or gate is running"


def integrator_run_section(repo: Path, now: float | None = None, runner: Runner = run_text,
                           state_dirs: list[Path] | None = None, memory: StepMemory | None = None,
                           waiting: Sequence[str] = (), remote: RemoteProbe | None = None,
                           queue: Sequence[Mapping[str, Any]] = (), prefer: int | None = None) -> dict:
    now = time.time() if now is None else now
    procs = sv.read_processes(runner)
    notes = [] if procs else ["ps could not be read: no live stage can be shown"]
    warns = [] if procs else [sv._warn("ps", "", notes[0])]
    local = sv.gate_section(procs, _default_state_dirs() if state_dirs is None else state_dirs, now)
    # The newest gate may run on another box: the top is the newest of this box's and the gate boxes'.
    here = os.uname().nodename + " (this box)"
    gate_boxes = gate_remote_boxes()
    remote_read = read_gate_boxes(remote, gate_boxes, now)
    where, gate, report = pick_gate(local, here, remote_read, prefer)
    # Lanes torn down for a batch, on every box read: steps of that batch, never a gate of their own.
    teardowns = [dict(t, box=os.uname().nodename) for t in (local or {}).get("teardowns") or [] if isinstance(t, dict)]
    for entry in remote_read:
        teardowns += [dict(t, box=entry["box"]) for t in (entry.get("gate") or {}).get("teardowns") or []
                      if isinstance(t, dict)]
    elsewhere = where != here
    if elsewhere and gate is not None:
        gate = _remote_top(where, gate, next(e.get("at") for e in remote_read if e["box"] == where), now)
        # The box's console report, sent as text: its paths are that box's, never read here.
        gate_log = sv.GateLogRef(Path(report["path"]), _num(report.get("started")), text=report["text"],
                              mtime=_num(report.get("mtime")), box=where) if report else None
    else:
        gate_log = sv.find_gate_log(procs, repo, memory, gate["label"] if gate and gate["state"] == "running" else None, now)
    if not gate_boxes:
        notes.append(GATE_BOXES_UNSET)
    for entry in remote_read:
        if entry.get("problem") and not entry["problem"].startswith(PROBE_PENDING):
            warns.append(sv._warn("gate-box", entry["box"], _gate_box_text(entry["box"], entry)))
    trunk = trunk_segments(repo, runner)
    started = gate_log.started if gate_log and gate_log.started else _num((gate or {}).get("started_at"))
    stale = stale_gate(gate, where, started, trunk[0])
    if stale:
        # Shown as current, its log and start would cut the ledger at its own batch, hours of batches back.
        warns.append(sv._warn("stale-gate", gate["label"], stale))
        gate_log, started = None, None
    batch = batch_seams(repo, runner, started, trunk)
    # No merge on the pushed trunk since the last ledger: the batch may be in a tree of the integrator's own.
    tree = None if batch["merges"] or batch.get("landed") else batch_tree(repo, procs, runner, memory)
    if tree:
        batch = {**batch, "merges": tree["merges"]}
    if trunk_ref(repo, runner) == "HEAD":
        notes.append(TRUNK_IS_HEAD)
    # The fast tests run where the batch is built, and leave their logs there.
    built_in = tree["path"] if tree else repo
    first = batch["merges"][0]["at"] if batch["merges"] else None
    unit = unit_lane(built_in, procs, first)
    merging = current_merge(procs)
    stage = stage_of(gate, unit, merging, merge_in_progress(repo))
    if memory is not None:
        live = [(f"merge:{merging}", merging)] if merging else []
        live += [(f"pass:{lp['label']}:{lp['lane']}", lp["label"]) for lp in (gate or {}).get("live_passes", [])]
        memory.observe(live, now)
    art_now = None if elsewhere else (gate_log.art_dir, gate_log.art_stem) if gate_log else sv._tree_artifacts(procs)
    running_boxes = sorted(sv._slice_boxes(art_now)) if gate and gate.get("state") == "running" else []
    box_status = sv.read_box_status(repo, running_boxes, now, runner) if running_boxes else {}
    for box, status in box_status.items():
        if status.get("problem"):
            warns.append(sv._warn("box-status", box, f"{box}: {status['problem']}"))
        elif status.get("state") == "running" and status.get("updated_at") \
                and now - status["updated_at"] > sv.BOX_QUIET_WARN_SECONDS:
            warns.append(sv._warn("box-quiet", box, f"{box} has published no status update for "
                               f"{sv.fmt_seconds(now - status['updated_at'])} while running"))
    box_live: dict[str, dict] = {}
    for box in running_boxes if remote is not None else []:
        got = remote.get(box, now)
        sections = (got.get("data") or {}).get("sections") or {}
        if sections:
            live = max(sections.values(), key=lambda sec: len(sec.get("rows", [])))
            box_live[box] = live
            warns += [dict(w, subject=f"{box}:{w['subject']}", text=f"{box}: {w['text']}")
                      for w in live.get("warnings", []) if w["code"] in ("red", "silent", "slow")]
        else:
            problem = got.get("problem") or "the box reported no running gate lanes"
            box_live[box] = {"problem": problem}
            if got.get("problem"):
                warns.append(sv._warn("box-probe", box, f"{box}: {problem}"))
    steps = sv.build_steps(repo, procs, None if stale else gate, batch, gate_log, memory, waiting, now, box_status,
                        box_live, remote_box=where if elsewhere else "", built_in=built_in)
    seams = [{"seam": m["seam"], "merged": sv._clock(m["at"]), "sha": m["sha"]} for m in batch["merges"]]
    if first is None:
        notes.append("no seam merges since the last ledger commit")
    # What the step list once showed as "N other pushed seams on the board": the seam branches pushed and not
    # merged into the trunk. They are the board, not this batch; said once, with what they are.
    board = [w for w in waiting if w not in batch.get("merged_all", ())]
    board_note = (f"Not in this batch: {len(board)} other seam branch(es) are pushed and not merged into the trunk. "
                  "Most are unfinished claims, stale bases or not rows; the Seam status table says which are "
                  "ready. They are not steps of this run." if board else "")
    batch_in = ({"batch_tree": {"name": tree["path"].name, "branch": tree["branch"]}} if tree else {})
    spans = ([{"step": "merges", "from": sv._clock(first), "to": sv._clock(batch["merges"][-1]["at"]),
               "took": sv.fmt_seconds(batch["merges"][-1]["at"] - first)}]
             if first is not None else [])
    return {"stage": stage, "stage_is": sv.step_meaning(stage),
            "next_batch": {"waiting": [{"seam": q.get("id", "?"), "waiting": q.get("age", "?"), "why": q.get("reason", "ready")}
                                       for q in queue]} if stage.startswith("between") else None,
            "batch": stale or (gate["label"] if gate else "no gate on record"),
            "gate_box": where if gate else "",
            "gate_boxes": ([_gate_box_text(here, {"gate": local})] + [_gate_box_text(e["box"], e) for e in remote_read]
                           if gate_boxes else []),
            **({"gate_stale": stale} if stale else {}),
            "last_ledger": batch["ledger"],
            "merging_now": merging or ("a git merge is open (MERGE_HEAD)" if merge_in_progress(repo) else None),
            "seams_in_batch": seams, **batch_in, "merge_timing": spans,
            "unit_lane": unit, "gate": gate if gate else {"state": "no gate on record"},
            "teardowns": teardowns, **({"board_note": board_note} if board_note else {}),
            "steps": steps, **({"steps_problem": memory.problem} if memory is not None and memory.problem else {}),
            "step_meanings": [{"step": start.strip(), "is": means} for start, means in sv.STEP_MEANINGS],
            "notes": notes, "warnings": warns + (gate["warnings"] if gate else [])}


def _default_state_dirs() -> list[Path]:
    root = Path.home() / ".local" / "state"
    try:
        return sorted(p for p in root.glob("skykeep-*") if p.is_dir())
    except OSError:
        return []
