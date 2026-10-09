"""The Board: one object owning the collectors' state, building the JSON document of named sections.
"""
from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from . import hub as sv
from .settings import page_time
from .sources.lease import run_cmd
from .sources.summaries import post_json

if TYPE_CHECKING:      # names used in annotations only (strings at runtime)
    from .sections.work import Runner
    from .settings import WebConfig
    from .sources.disk import DiskUsage
    from .sources.loops_memory import LoopUnit
    from .sources.summaries import Transport


#: How many landed rows the page keeps of the service's list: it draws the days, not the rows.
LANDING_ROWS_SHOWN = 20


def _slim(sections: dict) -> None:
    """Drop what the page would carry twice or never draw, once every reader of the full sections has run.

    The queue's lines travel inside the Queue section, so the copy under its own key keeps only its
    standing; the landing rows fed the build line and the health strip already, and the page draws the
    per-day counts. Measured 2026-10-06: these two were 380 KB of a 1.2 MB state document.
    """
    lines = sections.get("queue_lines")
    if isinstance(lines, dict) and lines.get("state") == "ok":
        sections["queue_lines"] = {key: lines[key] for key in ("state", "read_at", "age") if key in lines}
    landing = sections.get("landing")
    if isinstance(landing, dict) and isinstance(landing.get("rows"), list):
        sections["landing"] = {**landing, "rows": landing["rows"][-LANDING_ROWS_SHOWN:],
                               "rows_total": len(landing["rows"])}


# ---------------------------------------------------------------- the board

class Board:
    """Owns the collectors' running state and builds the page's sections."""

    def __init__(self, wcfg: WebConfig, runner: Runner = run_cmd, summary_transport: Transport = post_json,
                 summary_spawn: Callable[[Callable[[], None]], None] | None = None) -> None:
        self.wcfg = wcfg
        self.runner = runner
        self.warning_log = sv.integrator_run.WarningLog()
        self.step_memory = sv.integrator_run.StepMemory()
        self.remote_probe = sv.integrator_run.RemoteProbe()
        #: Every finished gate and fast-test run seen, for the plan's history; and the integrator log's last text.
        self.run_history = sv.integrator_run.run_plan.RunHistory()
        self.integrator_log: str | None = None
        #: None when no summary endpoint is configured: then no box, and nothing is ever sent.
        self.summaries = None if wcfg.summary is None else sv.Summaries(
            wcfg.summary, wcfg.summary_timeout_seconds, wcfg.summary_reuse_seconds,
            int(wcfg.summary_max_tokens), transport=summary_transport, spawn=summary_spawn)
        self.scanner = sv.TranscriptScanner()
        self.sampler = sv.OllamaSampler()
        self.codex = sv.CodexReader()
        self.disk = sv.DiskSampler(wcfg.runout_window_minutes * 60, wcfg.runout_min_span_minutes * 60,
                                wcfg.disk_sample_seconds)
        self.lock = threading.Lock()
        self._slow: dict[str, tuple[float, Any]] = {}
        self._slow_lock = threading.Lock()
        self.cleanup = sv.Cleanup(wcfg.plan_ttl_seconds,
                               lambda now: sv.cleanup_context(wcfg, runner, self.loop_units, now))
        #: The todo service's top warnings, read beside the page (never inside a refresh).
        self.todo = sv.TodoWatch.from_config(wcfg)
        #: The todo service's documents (alarms, queue, integration), read beside the page the same way.
        self.todo_reader = sv.TodoReader.from_config(wcfg)

    def _cached(self, key: str, now: float, read: Callable[[], Any]) -> Any:
        """A slow reading (systemd, journals, git history) at most every SLOW_REFRESH_SECONDS."""
        with self._slow_lock:
            hit = self._slow.get(key)
            if hit is not None and 0 <= now - hit[0] < self.wcfg.slow_refresh_seconds:
                return hit[1]
            value = read()
            self._slow[key] = (now, value)
            return value

    def loop_units(self, now: float) -> list[LoopUnit]:
        def read() -> list[LoopUnit]:
            names = self.wcfg.loop_units or tuple(sv.list_loop_units(self.wcfg.loop_unit_glob, self.runner))
            return [sv.read_loop_unit(name, self.wcfg.journal_minutes, self.runner) for name in names]
        return self._cached("units", now, read)

    def sample_disk(self, now: float | None = None) -> DiskUsage | None:
        """One disk sample for the prediction; the page's refreshes and a timer both call it."""
        usage = sv.read_disk(self.wcfg.sv.repo, self.wcfg.zpool, self.runner)
        self.disk.add(time.time() if now is None else now, usage)
        return usage

    def fetch_seams(self, cfg, now: float) -> None:
        """`git fetch <remote> --prune`, throttled like every other slow reading (`_cached`).

        The terminal view never fetches (`scripts/sessionview_web.py`'s own doc);
        the Integrator box is what the integrator acts on, so its refs must
        be current rather than whatever somebody last fetched by hand. A
        failed fetch (offline, no remote configured) is silent: the box then
        shows what the last successful fetch left, same as any other slow
        reading that could not be taken this round.
        """
        self._cached("fetch", now, lambda: self.runner(
            ["git", "fetch", cfg.remote, "--prune"], cwd=cfg.repo, timeout=30.0))

    def state(self) -> dict:
        with self.lock:
            return self.build()

    def _fleet(self, cfg, now: float) -> dict:
        try:
            got = sv.integrator_run.fleet_section(sv.integrator_run.run_text, self.remote_probe, now)
            seams = self._cached("fleet_seams", now, lambda: sv.integrator_run.seams_by_box(
                cfg.repo, sv.integrator_run.run_text, sv.integrator_run.fleet_boxes()))
            return {"fleet": got["boxes"], "fleet_procs": got["procs"], "fleet_seams": seams}
        except Exception as exc:  # noqa: BLE001 - the box says it could not be read
            return {"fleet": [{"box": "fleet", "note": f"fleet view failed: {type(exc).__name__}"}]}

    def _integrator_run(self, cfg, now: float, waiting: list[dict] = ()) -> dict:
        """The integrator-run box; a collector failure is said in the box, never a failed refresh."""
        try:
            plan = sv.integrator_run.run_plan
            self.integrator_log = plan.read_integrator_log()
            box = sv.integrator_run.integrator_run_section(
                cfg.repo, now, memory=self.step_memory, waiting=[row["id"] for row in waiting],
                remote=self.remote_probe, queue=waiting, prefer=plan.current_batch(self.integrator_log))
        except Exception as exc:  # noqa: BLE001 - the box says it could not be read
            text = f"integrator view failed: {type(exc).__name__}"
            box = {"stage": "unreadable", "notes": [text],
                   "warnings": [{"code": "collector", "subject": "", "text": text}]}
        self.warning_log.observe(box.get("warnings", []), now)
        return box

    def _plan(self, cfg, sections: dict, now: float) -> dict:
        """Every step of the integration run with its history; a failure is said in the box, never a failed refresh."""
        gates = sv.integrator_run
        try:
            here = os.uname().nodename
            self.run_history.read_local(sv.batch_history.log_roots(cfg.repo), here)
            patterns = [p for p in os.environ.get(gates.GATE_REPORTS_VAR, "").split(os.pathsep) if p.strip()]
            self.run_history.pull(gates.gate_remote_boxes(), patterns, now)
            self.run_history.save()
            boxes = list(dict.fromkeys([here, *gates.gate_remote_boxes(), *gates.fleet_boxes()]))
            return gates.run_plan.integration_plan(sections["integrator_run"], sections, self.run_history,
                                                   self.integrator_log, now, here=here, boxes=boxes)
        except Exception as exc:  # noqa: BLE001 - the box says it could not be read
            return {"problem": f"the integration plan could not be built: {type(exc).__name__}: {exc}"[:300]}

    def _batch_history(self, cfg, now: float) -> dict:
        """The Previous Batch Stats box; a collector failure is said in the box, never a failed refresh."""
        try:
            return self._cached("batch_history", now, lambda: sv.batch_history.batch_history_section(
                cfg.repo, int(self.wcfg.batch_history_shown), now=now))
        except Exception as exc:  # noqa: BLE001 - the box says it could not be read
            return {"detail": f"batch history failed: {type(exc).__name__}", "rows": []}

    def build(self) -> dict:
        cfg = self.wcfg.sv
        self.fetch_seams(cfg, time.time())
        frame = sv.gather(cfg, self.scanner, self.sampler, self.codex)
        sv.apply_review_reasons(frame.seams, self.wcfg.reviews_dir, cfg.repo, cfg.remote)
        workers = sv.read_grok_workers(cfg.grok_home)
        items = sv.read_todo_items(cfg.todo)
        todo_ids = {item.row_id for item in items}
        usage = self.sample_disk(frame.now)
        prediction = self.disk.predict(frame.now, usage, self.wcfg.disk_line_pct)
        units = self.loop_units(frame.now)
        floor, floor_source = sv.memory_floor(self.wcfg.mem_floor_gib, units)
        memory = sv.memory_section(sv.read_meminfo(), floor, floor_source, sv.ProcTable.read(),
                                sv.memory_roots(frame, units, workers))
        throughput = self._cached("throughput", frame.now,
                                  lambda: sv.read_throughput(cfg.repo, frame.now, self.runner))
        claimed = self._cached("claims", frame.now, lambda: sv.in_process_section(cfg))
        steps = sv.read_loop_steps(cfg)
        session_agents = sv.read_session_agents(
            cfg, frame.sessions, frame.agents, frame.worktrees)
        sections = {
            "work": sv.work_split(items, frame.seams, frame.now),
            "seams": sv.seams_section(frame.seams, frame.fetched, frame.now, todo_ids),
            **self._fleet(cfg, frame.now),
            "integrator_run": self._integrator_run(cfg, frame.now, sv.integrator_section(frame.seams, frame.now)),
            "run_warnings": self.warning_log.section(),
            "batch_history": self._batch_history(cfg, frame.now),
            "integrator": sv.integrator_section(frame.seams, frame.now),
            "in_process": claimed,
            "next_up": sv.next_up_section(cfg, {row["id"] for row in claimed}),
            "subagents": sv.subagents_section(session_agents, steps, frame.now),
            "who_where": sv.who_where_section(session_agents, frame.agents, frame.now),
            "userquestions": sv.userquestions_section(self.wcfg.unresolved, cfg.repo / sv.claims.claim.BRIEFS,
                                                   steps, frame.now),
            "disk": sv.disk_section(usage, prediction, self.wcfg.disk_line_pct),
            "memory": memory,
            "loops": [sv.loop_facts(unit) for unit in units],
            "unresolved": sv.read_unresolved(self.wcfg.unresolved),
            "throughput": throughput,
            "sessionview": sv.sessionview_panels(cfg, frame, workers),
            "critical_path": sv.critical_path_section(self.wcfg.timing_data_dir, frame.now),
            "overseer": sv.overseer_section(self.wcfg.overseer_status, frame.now,
                                             self.wcfg.overseer_stale_minutes),
        }
        usage = sv.claude_usage(self.wcfg, units, frame.now)
        sections["usage"] = usage
        sections["barriers"] = sv.assemble_barriers(self.wcfg, sections, usage, throughput)
        sections["cleanup"] = sv.cleanup_section(self.wcfg)
        if self.summaries is not None:
            self.summaries.observe(sections)          # before the summaries join: one never feeds another
            sections["summaries"] = self.summaries.section(frame.now)
        alarm = sv.metered_alarm(self.wcfg, sections["seams"], sections["integrator"],
                              sv.lanes_live())
        # The todo service's documents: each answers at once from its last reading, or says why not.
        for key in sv.TODO_DOCUMENTS:
            sections[key] = self.todo_reader.section(key, frame.now)
        sections["help"] = sv.help_section(self.wcfg, os.environ)
        todo_warnings = self.todo.warnings(frame.now)
        lease = self._cached("integrator_lease", frame.now,
                             lambda: sv.integrator_lease_banner(self.wcfg, frame.now))
        # Health reads every other section of this same state, so it is built last.
        sections["health"] = sv.health_section(
            sections, todo_warnings, lease, frame.now,
            lease_seconds=self.wcfg.todo_untaken_minutes * 60,
            sync_stale_seconds=self.wcfg.todo_sync_stale_minutes * 60,
            slot_models=self.wcfg.slot_models)
        # The build line reads the same sections and the flags just raised from them.
        sections["flow"] = sv.flow_section(sections, sections["health"]["flags"], frame.now,
                                           lease_seconds=self.wcfg.todo_untaken_minutes * 60)
        run = sections.get("integrator_run")
        if isinstance(run, dict):
            # A copy: the section may be a collector's kept answer, and the inset is built from two sections.
            sections["integrator_run"] = {**run, "batch_now": sv.batch_now(sections)}
            sections["integrator_run"]["plan"] = self._plan(cfg, sections, frame.now)
        _slim(sections)
        return {
            "todo_warnings": todo_warnings,
            "alarm": alarm,
            "integrator_lease": lease,
            "generated": frame.now,
            "generated_text": page_time(frame.now, "%Y-%m-%d %H:%M:%S"),
            "interval_ms": max(1000, int(cfg.interval * 1000)),
            "repo": str(cfg.repo),
            "pages": sv.page_list(),
            "sections": sections,
        }
