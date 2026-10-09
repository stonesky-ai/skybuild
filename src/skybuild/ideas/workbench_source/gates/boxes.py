"""What the other boxes publish: their status refs in the box-refs repository (`git init --bare`, `git fetch --force`).
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import hub as sv
from .processes import run_text

if TYPE_CHECKING:      # names used in annotations only (strings at runtime)
    from .processes import Runner


# ---------------------------------------------------------------- what the other boxes publish

#: The other boxes are read through git refs only (ssh to them is refused): re-read at most this often.
#: A box runner with progress on publishes each suite as it ends (GATE-BOX-RUNNER-PUBLISHES-EACH-SUITE-
#: AS-IT-ENDS), and the page asks for its state every 15 s by default: this plus that keeps a published
#: suite on the page within about 30 s. The read happens only while a gate with remote slices runs, so
#: this is one small fetch of the status refs per page refresh then, and none otherwise.
BOX_STATUS_TTL_SECONDS = 10.0
#: A box that has published no status update for this long while it runs is warned as silent.
BOX_QUIET_WARN_SECONDS = 45 * 60
_box_cache: dict[str, Any] = {"at": 0.0, "key": None, "value": {}}


def box_refs_dir() -> Path:
    named = os.environ.get("SESSIONVIEW_WEB_BOX_REFS", "").strip()
    return Path(named) if named else Path.home() / ".local" / "state" / "skykeep-sessionview" / "box-refs.git"


def read_box_status(repo: Path, boxes: Sequence[str], now: float, runner: Runner = run_text,
                    refs_dir: Path | None = None) -> dict[str, dict]:
    """box -> what its `pending/gate-status-<box>` ref says: state, its own start, lanes, size, last update.

    Read through a bare repo of this view's own, never the checkout's object store. A ref that cannot be
    fetched says why instead of being left out. Cached for BOX_STATUS_TTL_SECONDS.
    """
    key = ",".join(sorted(boxes))
    if _box_cache["key"] == key and now - _box_cache["at"] < BOX_STATUS_TTL_SECONDS:
        return _box_cache["value"]
    out: dict[str, dict] = {}
    refs = refs_dir or box_refs_dir()
    url = runner(["git", "-C", str(repo), "remote", "get-url", "origin"]).strip()
    if not url:
        out = {b: {"problem": "origin's URL could not be read, so the box's status ref cannot be fetched"} for b in boxes}
    else:
        if not (refs / "HEAD").exists():
            runner(["git", "init", "-q", "--bare", str(refs)])
        specs = [f"refs/heads/pending/gate-status-{b}:refs/s/{b}" for b in boxes if re.fullmatch(r"\w+", b)]
        runner(["git", "-C", str(refs), "fetch", "-q", "--force", url, *specs])
        for box in boxes:
            if not re.fullmatch(r"\w+", box):
                out[box] = {"problem": "box name not usable as a ref name"}
                continue
            evidence = runner(["git", "-C", str(refs), "show", f"refs/s/{box}:GATE-EVIDENCE.json"])
            declaration = runner(["git", "-C", str(refs), "show", f"refs/s/{box}:GATE-DECLARATION.json"])
            log = runner(["git", "-C", str(refs), "log", "--format=%ct\t%s", f"refs/s/{box}", "-8"])
            try:
                ev = json.loads(evidence) if evidence.strip() else {}
                dec = json.loads(declaration) if declaration.strip() else {}
            except ValueError:
                out[box] = {"problem": "the box's status files are not readable JSON"}
                continue
            if not ev and not dec and not log.strip():
                out[box] = {"problem": "the box's status ref could not be fetched (no such ref yet, or no network)"}
                continue
            commits = [(float(t), subj) for t, _, subj in (ln.partition("\t") for ln in log.splitlines()) if t.isdigit()]
            # A running box's suites finished so far: its progress, never its result. None: it publishes none.
            done = ev.get("suites_done") if ev.get("state") == "running" else None
            out[box] = {"state": ev.get("state", "no evidence yet"), "started_at": ev.get("started_at"),
                        "first_lane": ev.get("first_lane", dec.get("first_lane")),
                        "lane_count": ev.get("lane_count"), "cores": dec.get("cores"),
                        "mem_gib": round(dec["mem_available_bytes"] / 2**30, 1) if dec.get("mem_available_bytes") else None,
                        "updated_at": commits[0][0] if commits else None,
                        "updates": [{"at": c[0], "what": c[1].split(": ", 1)[-1][:90]} for c in commits],
                        "slice_id": ev.get("slice_id"), "finished_at": ev.get("finished_at"),
                        "wall_seconds": ev.get("wall_seconds"), "gate_exit": ev.get("gate_exit"),
                        "suites": _verdicts(ev.get("suites")),
                        "suites_done": _verdicts(done) if isinstance(done, list) else None}
    _box_cache.update(at=now, key=key, value=out)
    return out


def _verdicts(items: Any) -> list[dict]:
    """A box record's verdict list, each cut to what the page shows."""
    return [{"suite": str(x.get("suite", "?")), "rc": x.get("rc"), "seconds": x.get("seconds"),
             "summary": str(x.get("summary", ""))[:200]}
            for x in (items or []) if isinstance(x, dict)]


def box_status_text(status: dict | None, now: float) -> str:
    if not status:
        return ""
    if status.get("problem"):
        return "box status unreadable: " + status["problem"]
    began = f"since {sv._stamp(status['started_at'], now)}" if status.get("started_at") else "start time unknown"
    lanes = (f"lanes {status['first_lane']}-{status['first_lane'] + status['lane_count'] - 1}"
             if status.get("first_lane") is not None and status.get("lane_count") else "")
    size = f"{status['cores']} cores" if status.get("cores") else ""
    age = f"last status update {sv.fmt_seconds(now - status['updated_at'])} ago" if status.get("updated_at") else ""
    note = ""
    if status.get("state") == "running":
        done = status.get("suites_done")
        if done is None:
            note = "the box publishes no per-suite progress, only its start and its result"
        else:
            reds = sum(1 for x in done if x.get("rc") != 0)
            note = (f"{len(done)} suite(s) finished so far" + (f", {reds} red" if reds else "")
                    + " (partial: the box's verdict is its complete record, not this list)")
    return "; ".join(x for x in (f"box says {status.get('state')} {began}", lanes, size, age, note) if x)


def _partial_suites(art: tuple[Path, str] | None, box: str, done: Sequence[Mapping[str, Any]]) -> list[dict]:
    """A running box's slice as suite rows: those it has published as finished with their verdicts, the rest
    pending. Each finished row says the box is still running: a partial list is never the box's result."""
    rows = sv._slice_suites(art, box)
    finished = {str(x.get("suite")): x for x in done}
    out = []
    for row in rows:
        hit = finished.pop(row["suite"], None)
        if hit is None:
            out.append(dict(row, text="handed to the box; not finished yet"))
            continue
        out.append({"state": "passed" if hit.get("rc") == 0 else "red", "suite": row["suite"],
                    "text": f"{hit.get('summary') or 'rc=' + str(hit.get('rc'))} (partial: the box is still running)"})
    return out
