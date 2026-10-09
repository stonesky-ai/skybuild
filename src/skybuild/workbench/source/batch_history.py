"""Previous Batch Stats: every past batch of the trunk, newest first, read-only.

A batch is read from the trunk's first-parent log: the commits after batch
N-1's last `Batch<N> ledger` commit up to batch N's last one. Its seams are the
`Merge seam/<Id>` commits in that span, its start is the first of those merges
and its end is the ledger commit. Several ledger commits under one batch number
are one batch. What else explains a batch's runtime (the gate's wall time, the
unit lane's time, suites red and re-run, the idle gap before it, minutes per
seam) is added only where its source can be read: a part with no readable
source is None here and "unknown" in the table, never 0.

Pure functions over a git runner and a list of log roots. The ledger and merge
patterns, the gate-log parser and the suite history are the integrator view's
own (the `gates` package beside this module), loaded from it and never copied.
"""

from __future__ import annotations

import html
import json
import os
import re
import statistics
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

#: The integrator view: its regexes, runner, gate-log parser and suite history are reused here.
from .gates import hub as integrator
from .gates.kept_warnings import default_warning_file

Runner = integrator.Runner

UNKNOWN = "unknown"
#: Gate-log suite states that count as red (the integrator view's own reading of a block).
_RED_STATES = ("red", "FAILED")


def stamp(value: float | None) -> str:
    """A date-time in the page's zone (Central by default) with CST or CDT beside it, or "unknown"."""
    if value is None:
        return UNKNOWN
    return integrator.zoned(value, "%Y-%m-%d %H:%M:%S")


def read_trunk(repo: Path, runner: Runner = integrator.run_text) -> list[tuple[float, str, str]]:
    """The trunk's first-parent commits, oldest first, as (epoch, sha, subject)."""
    ref = integrator.trunk_ref(repo, runner)
    out = runner(["git", "-C", str(repo), "log", ref, "--first-parent", "--format=%ct\t%h\t%s"])
    commits = []
    for line in out.splitlines():
        at, _, rest = line.partition("\t")
        sha, _, subject = rest.partition("\t")
        if at.isdigit():
            commits.append((float(at), sha, subject))
    commits.reverse()
    return commits


#: The ledger commits of the eras before `Batch<N> ledger`: `batch<N>: ...` (batches 16 to 24), and before
#: those `Ledger: land ...` with no number at all. A tooling batch's ledger has no number either.
_OLD_NUMBERED = re.compile(r"^batch(\d+): ")
_UNNUMBERED = re.compile(r"^[Ll]edger: (?:land|close) ")
TOOLING_BATCH, EARLY_LANDING = "tooling batch", "landing (no number)"


def batch_name(batch: dict) -> str:
    return f"Batch{batch['number']}" if batch.get("number") is not None else str(batch.get("label") or "batch")


def numbered(batches: Sequence[dict]) -> list[dict]:
    """The batches with a number: the charts place a batch by it, and the kept timeline is filed under it."""
    return [b for b in batches if b.get("number") is not None]


def group_batches(commits: Sequence[tuple[float, str, str]]) -> list[dict]:
    """Past batches, newest first. Merges newer than the newest ledger commit belong to no past batch.

    Every landing the trunk records is one: a numbered batch in either wording, a tooling batch, and a
    landing from before batches had numbers. One with no number is listed only when it merged a seam.
    """
    batches: list[dict] = []
    pending: list[dict] = []
    for at, sha, subject in commits:
        ledger = integrator._LEDGER.match(subject) or _OLD_NUMBERED.match(subject)
        label = None if ledger else (TOOLING_BATCH if integrator._TOOLING_LEDGER.match(subject)
                                     else EARLY_LANDING if _UNNUMBERED.match(subject) else None)
        if label:
            # Its merges are its own: left pending, they would be counted into the numbered batch after it.
            if pending:
                batches.append({"number": None, "label": label, "merges": pending, "end": at, "ledgers": [sha],
                                "previous_end": batches[-1]["end"] if batches else None})
            pending = []
            continue
        if ledger:
            number = int(ledger.group(1))
            if batches and batches[-1]["number"] == number:
                batches[-1]["merges"].extend(pending)
                batches[-1]["end"] = at
                batches[-1]["ledgers"].append(sha)
            else:
                batches.append({"number": number, "merges": pending, "end": at, "ledgers": [sha],
                                "previous_end": batches[-1]["end"] if batches else None})
            pending = []
            continue
        merged = integrator._MERGE.match(subject)
        if merged:
            pending.append({"seam": merged.group(1), "sha": sha, "at": at})
    for batch in batches:
        merges = batch["merges"]
        start = merges[0]["at"] if merges else None
        # A ledger commit dated before its first merge (a rebased or skewed date) measures nothing.
        runtime = batch["end"] - start if start is not None and batch["end"] >= start else None
        previous = batch["previous_end"]
        batch.update(
            seams=[m["seam"] for m in merges], start=start, runtime=runtime,
            idle=start - previous if start is not None and previous is not None and start >= previous else None,
            per_seam_minutes=runtime / len(merges) / 60.0 if runtime is not None and merges else None,
            gate_seconds=None, unit_seconds=None, suites_red=None, suites_rerun=None)
    batches.reverse()
    return batches


def past_batches(repo: Path, runner: Runner = integrator.run_text) -> list[dict]:
    return group_batches(read_trunk(repo, runner))


def log_roots(repo: Path) -> list[Path]:
    """Where a batch's gate and unit logs land: the repo, the named gate roots, the scratch checkouts."""
    roots = [repo]
    for named in os.environ.get("SESSIONVIEW_WEB_GATE_ROOTS", "").split(os.pathsep):
        if named.strip():
            roots.append(Path(named.strip()))
    roots += integrator._scratch_roots()
    return list(dict.fromkeys(roots))


def read_run_logs(roots: Sequence[Path]) -> dict[str, list[dict]]:
    """Every readable gate master log and unit-lane log under the roots' `test-logs/`.

    A gate log counts only when the integrator view's parser reads a gate run in
    it; a unit log only when it carries pytest's verdict line with its duration.
    """
    gates: list[dict] = []
    units: list[dict] = []
    for root in roots:
        try:
            gate_paths = sorted((root / "test-logs").glob("gates-*.log"))
            unit_paths = sorted((root / "test-logs").glob("testfast-unit-*.log"))
        except OSError:
            continue
        for path in gate_paths:
            started = integrator.gate_log_started(path)
            try:
                ended = path.stat().st_mtime
            except OSError:
                continue
            parsed = integrator.parse_gate_log(integrator._read(path))
            if started is None or not parsed["label"] or not parsed["blocks"] or ended < started:
                continue
            red = {s["suite"] for block in parsed["blocks"] for s in block["suites"] if s["state"] in _RED_STATES}
            rerun = {suite for entry in parsed["reruns"] for suite in entry["suites"]}
            gates.append({"started": started, "seconds": ended - started, "red": red, "rerun": rerun})
        for path in unit_paths:
            started = integrator.gate_log_started(Path(path.name.replace("testfast-unit-", "gates-")))
            hit = None
            for hit in integrator._UNIT_RESULT.finditer(integrator._read(path)[-3000:]):
                pass
            if started is None or hit is None:
                continue
            units.append({"started": started, "seconds": float(hit.group(3))})
    return {"gates": gates, "units": units}


def add_parts(batches: Sequence[dict], logs: dict[str, list[dict]], history: dict[str, list[dict]] | None = None) -> None:
    """Fill each batch's parts from the logs that began inside its own span; none readable leaves None."""
    for batch in batches:
        start, end = batch["start"], batch["end"]
        if start is None or batch["runtime"] is None:
            continue
        gates = [g for g in logs["gates"] if start <= g["started"] <= end]
        units = [u for u in logs["units"] if start <= u["started"] <= end]
        if gates:
            batch["gate_seconds"] = sum(g["seconds"] for g in gates)
            batch["suites_red"] = len(set().union(*(g["red"] for g in gates)))
            batch["suites_rerun"] = len(set().union(*(g["rerun"] for g in gates)))
        elif history:
            # No master log: the lane logs' own suite lines still say which suites went red in the span.
            runs = [(suite, run) for suite, rows in history.items() for run in rows if start <= run["at"] <= end]
            if runs:
                batch["suites_red"] = len({suite for suite, run in runs if run["rc"] != 0})
        if units:
            batch["unit_seconds"] = sum(u["seconds"] for u in units)


def _suite_history(state_dirs: list[Path], now: float) -> dict[str, list[dict]]:
    """The integrator view's suite history, read without evicting that view's own cached reading.

    Its cache holds one key; the running gate's view reads with its label skipped, and a read from
    here under another key would make every refresh of that view re-read every lane log.
    """
    kept = dict(integrator._history_cache)
    try:
        return integrator.duration_history(state_dirs, now)
    finally:
        integrator._history_cache.update(kept)


def _count(value: int | None) -> str:
    return UNKNOWN if value is None else str(value)


def table_rows(batches: Sequence[dict]) -> list[dict]:
    """One row of plain strings per batch. A seam id is carried as data: the page and the markdown escape it."""
    rows = []
    for batch in batches:
        seams = batch["seams"]
        per_seam = batch["per_seam_minutes"]
        rows.append({
            "batch": batch_name(batch),
            "seam count": str(len(seams)),
            "seams": ", ".join(seams) if seams else "none",
            "start": stamp(batch["start"]),
            "end": stamp(batch["end"]),
            "runtime": integrator.fmt_seconds(batch["runtime"]),
            "gate wall": integrator.fmt_seconds(batch["gate_seconds"]),
            "fast tests": integrator.fmt_seconds(batch["unit_seconds"]),
            "suites red": _count(batch["suites_red"]),
            "suites re-run": _count(batch["suites_rerun"]),
            "idle before": integrator.fmt_seconds(batch["idle"]),
            "merges from": stamp(batch.get("merges_from")),
            "merges to": stamp(batch.get("merges_to")),
            "fast tests start": stamp(batch.get("unit_start")) if batch.get("unit_start") is not None else "no fast-test log",
            "fast tests end": stamp(batch.get("unit_end")) if batch.get("unit_end") is not None else "no fast-test log",
            "gate start": stamp(batch.get("gate_start")) if batch.get("gate_start") is not None else "no gate log",
            "gate end": stamp(batch.get("gate_end")) if batch.get("gate_end") is not None else "no gate log",
            "gate log": batch.get("gate_detail") or batch.get("gate_label") or "no gate log",
            "ledger at": stamp(batch.get("ledger_at")),
            "minutes per seam": UNKNOWN if per_seam is None else f"{per_seam:.1f}",
        })
    return rows


class KeptBatchTimeline:
    """Keep observed log boundaries on disk so a restart does not erase past batch timings."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_warning_file().with_name("batch_history.json")
        self.lock = threading.Lock()
        self.entries: dict[str, dict] = {}
        self.unreadable = False
        try:
            data = json.loads(self.path.read_text())
            if isinstance(data, dict) and isinstance(data.get("batches"), dict):
                self.entries = {str(k): v for k, v in data["batches"].items() if isinstance(v, dict)}
        except (FileNotFoundError, NotADirectoryError):
            pass
        except (OSError, ValueError):
            self.unreadable = True

    def keep(self, key: str, values: dict, *, save: bool = True) -> dict:
        with self.lock:
            prior = self.entries.get(key, {})
            before = dict(prior)
            prior.update({k: v for k, v in values.items() if v is not None})
            self.entries[key] = prior
            self.changed = getattr(self, "changed", False) or prior != before
            kept = dict(prior)
        if save:
            self.save()
        return kept

    def save(self) -> None:
        """Write the kept boundaries, once, and only when a `keep` changed one."""
        with self.lock:
            if self.unreadable or not getattr(self, "changed", False):
                return
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_name(self.path.name + ".tmp")
                tmp.write_text(json.dumps({"batches": self.entries}, indent=1))
                os.chmod(tmp, 0o600)
                os.replace(tmp, self.path)
                self.changed = False
            except OSError:
                pass


def add_timeline(batches: Sequence[dict], logs: dict[str, list[dict]], kept_file: Path | None = None) -> None:
    kept = KeptBatchTimeline(kept_file)
    for batch in numbered(batches):
        first, last = batch["start"], batch["end"]
        if first is None or batch["runtime"] is None:
            continue
        gates = sorted((g for g in logs["gates"] if first <= g["started"] <= last), key=lambda g: g["started"])
        units = sorted((u for u in logs["units"] if first <= u["started"] <= last), key=lambda u: u["started"])
        values = {"merges_from": batch["merges"][0]["at"],
                  "merges_to": batch["merges"][-1]["at"],
                  "ledger_at": batch["end"], "idle_before": batch["idle"]}
        if gates:
            values.update(gate_start=gates[0]["started"], gate_end=gates[-1]["ended"],
                          gate_label=gates[0].get("label"), gate_detail=gates[-1].get("detail"))
        if units:
            values.update(unit_start=units[0]["started"], unit_end=units[-1]["ended"])
        batch.update(kept.keep(str(batch["number"]), values, save=False))
    kept.save()


def batch_history_section(repo: Path, shown: int, runner: Runner = integrator.run_text,
                          roots: Sequence[Path] | None = None, state_dirs: Sequence[Path] | None = None,
                          now: float | None = None, kept_file: Path | None = None) -> dict[str, Any]:
    """The page's Previous Batch Stats box: every past batch the trunk knows, or one line saying why there are none.

    `shown` is the caller's setting (ADR-0002); there is no default count here. It is how many batches,
    newest first, the table and the timeline show before their "show all" (owner order 2026-10-06: the
    whole history, "back to the beginning of time, whatever is known", abbreviated behind a button); 0
    still switches the box off.
    """
    commits = read_trunk(repo, runner)
    if not commits:
        return {"detail": "the trunk's first-parent log could not be read: no past batch can be shown", "rows": []}
    batches = group_batches(commits)
    if not batches:
        return {"detail": "no `Batch<N> ledger` commit on the trunk yet: no past batch to show", "rows": []}
    first = max(0, int(shown))
    if not first:
        # The dial at 0 switches the table off, and the box says so rather than reading as an empty trunk.
        return {"detail": f"the shown-batch setting is 0: none of the {len(batches)} past batches is listed",
                "rows": []}
    dirs = integrator._default_state_dirs() if state_dirs is None else list(state_dirs)
    history = _suite_history(dirs, time.time() if now is None else now) if dirs else None
    named_roots = log_roots(repo) if roots is None else list(roots)
    add_parts(batches, read_run_logs(named_roots), history)
    add_timeline(batches, integrator.batch_timeline_logs(named_roots), kept_file)
    newest = numbered(batches)[:first]
    oldest = batches[-1]
    section = {"detail": f"all {len(batches)} batches the trunk records, newest first, back to {batch_name(oldest)}"
                         f" ({stamp(oldest['end'])}); runtime is first merge to ledger commit; "
                         f"a part with no readable source reads {UNKNOWN}; a batch with no number is in the "
                         "table and not in the charts",
               "shown": first, "total": len(batches),
               "rows": table_rows(batches),
               "charts": {"runtime by batch": svg_spec(runtime_line_chart(batches)),
                          "runtime by seam count": svg_spec(seam_scatter_chart(batches)),
                          f"where the time went, newest {len(newest)} batches": svg_spec(timeline_bar_chart(newest))}}
    if len(numbered(batches)) > len(newest):
        section["charts_all"] = {f"where the time went, all {len(numbered(batches))} numbered batches":
                                 svg_spec(timeline_bar_chart(batches))}
    return section


# ---------------------------------------------------------------- the two charts

_W, _H, _PAD_L, _PAD_B, _PAD_T, _PAD_R = 520, 260, 64, 44, 18, 16


def _ticks(low: float, high: float, count: int = 5) -> list[float]:
    span = (high - low) or 1.0
    return [low + span * i / (count - 1) for i in range(count)]


def _scale(value: float, low: float, high: float, out_low: float, out_high: float) -> float:
    span = (high - low) or 1.0
    return out_low + (value - low) / span * (out_high - out_low)


def _frame(title: str, x_title: str, y_title: str, xs: Sequence[float], ys: Sequence[float],
           x_fmt, y_fmt) -> tuple[list[str], tuple[float, float, float, float]]:
    """Axes, tick labels and axis titles; the shared part of both charts. Returns the parts and the data box."""
    x_low, x_high, y_high = min(xs), max(xs), max(ys)
    y_low = 0.0
    parts = [(f'<svg xmlns="http://www.w3.org/2000/svg" class="chart" viewBox="0 0 {_W} {_H}" role="img" '
              f'aria-label="{html.escape(title)}">'),
             f"<title>{html.escape(title)}</title>",
             f'<line class="axis" x1="{_PAD_L}" y1="{_H - _PAD_B}" x2="{_W - _PAD_R}" y2="{_H - _PAD_B}"/>',
             f'<line class="axis" x1="{_PAD_L}" y1="{_PAD_T}" x2="{_PAD_L}" y2="{_H - _PAD_B}"/>']
    for tick in _ticks(x_low, x_high):
        x = _scale(tick, x_low, x_high, _PAD_L, _W - _PAD_R)
        parts.append(f'<text class="tick" x="{x:.1f}" y="{_H - _PAD_B + 14}" text-anchor="middle" font-size="10">'
                     f"{html.escape(x_fmt(tick))}</text>")
    for tick in _ticks(y_low, y_high):
        y = _scale(tick, y_low, y_high, _H - _PAD_B, _PAD_T)
        parts.append(f'<line class="grid" x1="{_PAD_L}" y1="{y:.1f}" x2="{_W - _PAD_R}" y2="{y:.1f}"/>')
        parts.append(f'<text class="tick" x="{_PAD_L - 6}" y="{y + 3:.1f}" text-anchor="end" font-size="10">'
                     f"{html.escape(y_fmt(tick))}</text>")
    parts.append(f'<text class="axis-title" x="{(_PAD_L + _W - _PAD_R) / 2:.0f}" y="{_H - 6}" text-anchor="middle" font-size="11">'
                 f"{html.escape(x_title)}</text>")
    parts.append(f'<text class="axis-title" x="12" y="{(_PAD_T + _H - _PAD_B) / 2:.0f}" text-anchor="middle" font-size="11" '
                 f'transform="rotate(-90 12 {(_PAD_T + _H - _PAD_B) / 2:.0f})">{html.escape(y_title)}</text>')
    return parts, (x_low, x_high, y_low, y_high)


def svg_spec(markup: str) -> dict | None:
    """A chart's SVG as plain data {tag, attrs, text, children}, so the page builds its nodes with
    createElementNS and never parses markup. "" (no chart) is None."""
    if not markup:
        return None

    def node(element) -> dict:
        return {"tag": element.tag.rpartition("}")[2], "attrs": dict(element.attrib),
                "text": (element.text or "").strip(), "children": [node(child) for child in element]}

    return node(ElementTree.fromstring(markup))


def _minutes(value: float) -> str:
    return f"{value / 60:.0f}"


def measured(batches: Sequence[dict]) -> list[dict]:
    """The batches with a measured runtime, oldest first: an unknown runtime is left off, never drawn at 0."""
    return sorted((b for b in numbered(batches) if b.get("runtime") is not None), key=lambda b: b["number"])


def runtime_line_chart(batches: Sequence[dict]) -> str:
    """x batch number, y runtime in minutes, a dashed median line. Empty string when nothing is measured."""
    rows = measured(batches)
    if not rows:
        return ""
    xs = [float(b["number"]) for b in rows]
    ys = [b["runtime"] for b in rows]
    parts, (x_low, x_high, y_low, y_high) = _frame(
        "Runtime by batch", "batch number", "runtime (minutes)", xs, ys, lambda v: f"{v:.0f}", _minutes)

    def pos(x: float, y: float) -> tuple[float, float]:
        return (_scale(x, x_low, x_high, _PAD_L, _W - _PAD_R), _scale(y, y_low, y_high, _H - _PAD_B, _PAD_T))

    points = [pos(x, y) for x, y in zip(xs, ys)]
    parts.append('<polyline class="series" fill="none" stroke-width="2" points="'
                 + " ".join(f"{x:.1f},{y:.1f}" for x, y in points) + '"/>')
    for (x, y), batch in zip(points, rows):
        parts.append(f'<circle class="dot" cx="{x:.1f}" cy="{y:.1f}" r="3.5"><title>Batch{html.escape(str(batch["number"]))}: '
                     f"{html.escape(integrator.fmt_seconds(batch['runtime']))}</title></circle>")
    median = statistics.median(ys)
    _, my = pos(x_low, median)
    parts.append(f'<line class="median" x1="{_PAD_L}" y1="{my:.1f}" x2="{_W - _PAD_R}" y2="{my:.1f}" '
                 f'stroke-dasharray="4 3"/>')
    parts.append(f'<text class="note" x="{_W - _PAD_R}" y="{my - 3:.1f}" text-anchor="end" font-size="10">median '
                 f"{html.escape(integrator.fmt_seconds(median))}</text>")
    parts.append("</svg>")
    return "".join(parts)


def seam_scatter_chart(batches: Sequence[dict]) -> str:
    """x seam count, y runtime in minutes, each dot labelled with its two-digit batch number."""
    rows = measured(batches)
    if not rows:
        return ""
    xs = [float(len(b["seams"])) for b in rows]
    ys = [b["runtime"] for b in rows]
    parts, (x_low, x_high, y_low, y_high) = _frame(
        "Runtime by seam count", "seams in the batch (count)", "runtime (minutes)", xs, ys,
        lambda v: f"{v:.0f}", _minutes)
    for batch, x_value, y_value in zip(rows, xs, ys):
        x = _scale(x_value, x_low, x_high, _PAD_L, _W - _PAD_R)
        y = _scale(y_value, y_low, y_high, _H - _PAD_B, _PAD_T)
        label = f"{batch['number'] % 100:02d}"
        parts.append(f'<circle class="dot" cx="{x:.1f}" cy="{y:.1f}" r="3.5"><title>Batch{batch["number"]}: '
                     f'{len(batch["seams"])} seams, {html.escape(integrator.fmt_seconds(y_value))}</title></circle>'
                     f'<text class="tick" x="{x + 5:.1f}" y="{y - 4:.1f}" font-size="9">{html.escape(label)}</text>')
    parts.append("</svg>")
    return "".join(parts)


#: The parts of a batch's time, in the order they are stacked, each with the class the style sheet colours.
TIMELINE_PARTS: tuple[tuple[str, str], ...] = (
    ("idle", "idle before"), ("merges", "merging"), ("unit", "fast tests"), ("gate", "gate"),
    ("unmeasured", "not broken down"))


def timeline_bar_chart(batches: Sequence[dict]) -> str:
    """One stacked bar per batch, in the order the time passed: the idle time before it, merging, the fast
    tests, the gate, and what is left of its runtime that no log breaks down. With a legend.

    The bars share one scale, so a long batch is a long bar; each part's time is in its tooltip, and the
    figure at a bar's end is the batch's runtime, the idle time before it not counted.
    """
    values = []
    for batch in numbered(batches):
        merge_time = (batch["merges"][-1]["at"] - batch["merges"][0]["at"]
                      if len(batch["merges"]) > 1 else 0)
        unit_time = max(0, batch["unit_end"] - batch["unit_start"]) if batch.get("unit_end") is not None and batch.get("unit_start") is not None else None
        gate_time = max(0, batch["gate_end"] - batch["gate_start"]) if batch.get("gate_end") is not None and batch.get("gate_start") is not None else None
        if batch.get("runtime") is not None:
            # No part is longer than the whole: a kept span that says so is cut to the runtime, or one
            # bad span would set the scale for every bar.
            merge_time, unit_time, gate_time = (None if value is None else min(value, batch["runtime"])
                                                for value in (merge_time, unit_time, gate_time))
        known = sum(value for value in (merge_time, unit_time, gate_time) if value)
        # What the runtime holds beyond its measured parts: all of it, when no part is measured.
        rest = max(0, (batch.get("runtime") or 0) - known)
        row = (batch["idle"], merge_time, unit_time, gate_time, rest)
        values.append((batch["number"], row, batch.get("runtime")))
    maximum = max((sum(v for v in row if v is not None) for _, row, _ in values), default=0)
    if maximum <= 0:
        return ""
    width, left, top, row_gap, right = 500, 62, 34, 20, 58
    plot = width - left - right
    height = top + row_gap * len(values) + 6
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" class="chart timeline" viewBox="0 0 {width} {height}" role="img" aria-label="Where each batch\'s time went">',
             "<title>Where each batch's time went</title>"]
    # The legend: a swatch and a word per part, so no part is told by its colour alone.
    cursor = float(left)
    for key, word in TIMELINE_PARTS:
        parts.append(f'<line class="seg seg-{key}" x1="{cursor:.1f}" y1="10" x2="{cursor + 12:.1f}" y2="10" stroke-width="10"/>')
        parts.append(f'<text class="tick" x="{cursor + 16:.1f}" y="13.5" font-size="10">{html.escape(word)}</text>')
        cursor += 26 + 5.8 * len(word)
    for i, (number, row, runtime) in enumerate(values):
        y = top + i * row_gap
        parts.append(f'<text class="tick" x="{left - 6}" y="{y + 3.5}" text-anchor="end" font-size="10">Batch{number}</text>')
        cursor = float(left)
        for (key, word), duration in zip(TIMELINE_PARTS, row):
            if not duration or duration < 0:
                continue
            segment = plot * duration / maximum
            # A 1.5-unit gap after each part keeps two parts apart whatever their colours.
            parts.append(f'<line class="seg seg-{key}" x1="{cursor:.1f}" y1="{y}" x2="{max(cursor + 0.5, cursor + segment - 1.5):.1f}" y2="{y}" '
                         f'stroke-width="12"><title>Batch{number}: {html.escape(word)} {html.escape(integrator.fmt_seconds(duration))}</title></line>')
            cursor += segment
        total = runtime if runtime is not None else sum(v for v in row[1:] if v is not None and v > 0)
        parts.append(f'<text class="note" x="{cursor + 4:.1f}" y="{y + 3.5}" font-size="9.5">{html.escape(integrator.fmt_seconds(total))}</text>')
    parts.append("</svg>")
    return "".join(parts)
