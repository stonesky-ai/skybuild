"""Throughput health: the one strip on every page, and the flags behind it.

Pure functions over the page's own sections (owner order 2026-10-05, the "fans on" causes): a
box with free resources and no work, a queue nobody can take, a dead claim, a stale seam scan, a
red preflight, a gate waiting for lanes, ready seams with no batch running, usage near the weekly
stop. Each flag names the cause and the fix in one line each, and where one command does the fix,
that command; the strip is red when any flag is red, amber when any is amber, green otherwise.
Every string is data for the page to show as text.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from .. import hub as sv
from .todo_watch import TODO_KEEPER_UNIT, TODO_SERVICE_UNIT

RED, AMBER, GREEN = "red", "amber", "green"
#: The sync timer that scans seams for the todo service (docs/dev/todo-service-install.md § The sync timer).
TODO_SYNC_UNIT = "skykeep-todo-sync.timer"
#: Kept-warning codes that mean a merge was refused before any lane ran.
PREFLIGHT_CODES = ("preflight", "dependency", "advisory", "dependency-advisory")
#: What the strip says when no flag is up.
ALL_CLEAR = "Build moving: nothing flagged"


def _health_flag(key: str, level: str, headline: str, cause: str, fix: str, command: str = "", box: str = "") -> dict:
    return {"key": key, "level": level, "box": box, "headline": headline, "cause": cause, "fix": fix,
            "command": command}


def _health_rows(value: object) -> list[dict]:
    return [row for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []


def _health_map(value: object) -> dict:
    return dict(value) if isinstance(value, Mapping) else {}


def _fleet_count(text: object) -> int | None:
    """The leading count of a fleet cell ("3 (longest 2h)" -> 3; "-" -> None: not read)."""
    head = str(text).split(" ", 1)[0]
    return int(head) if head.isdigit() else None


def _fleet_idle_boxes(fleet: list[dict]) -> list[dict]:
    """Fleet rows whose every agent count is zero: a box that runs nothing."""
    out = []
    for row in fleet:
        counts = [_fleet_count(row.get(kind)) for kind in ("claude", "claude_headless", "codex", "grok")]
        if counts and all(count == 0 for count in counts):
            out.append(row)
    return out


def _fleet_box_name(row: Mapping) -> str:
    return str(row.get("box", "?")).split(" ", 1)[0]


def health_flags(sections: Mapping, todo_warnings: object, lease: Mapping | None, now: float, *,
                 lease_seconds: float, sync_stale_seconds: float, slot_models: Sequence[str] | None,
                 usage_warn_margin_pct: float = 5.0) -> list[dict]:
    """Every flag up now, red first; [] when the build needs nothing from anyone."""
    flags: list[dict] = []

    for warning in _health_rows(todo_warnings):
        flags.append(_health_flag("todo_service", RED, str(warning.get("headline", "todo service warning")),
                           "the todo service is what hands out work", str(warning.get("fix", ""))))

    alarms = _health_map(sections.get("alarms"))
    open_alarms = [a for a in _health_rows(alarms.get("alarms")) if a.get("acked_at") is None]
    if open_alarms:
        errors = sum(1 for a in open_alarms if a.get("level") == "error")
        flags.append(_health_flag("alarms", RED if errors else AMBER,
                           f"{len(open_alarms)} alarm(s) open" + (f", {errors} error(s)" if errors else ""),
                           str(open_alarms[0].get("subject", "")),
                           "Read each on the Alarms page and ack it there once it is handled"))

    if isinstance(lease, Mapping) and lease.get("state") in ("absent", "stale", "unreadable"):
        flags.append(_health_flag("integrator", RED, str(lease.get("headline", "NO INTEGRATOR RUNNING")),
                           str(lease.get("detail", "")),
                           "Start the integrator session, or renew the seat it holds",
                           "python3 scripts/agents/skybus/lease.py holder"))

    queue = _health_map(sections.get("queue"))
    if queue.get("state") == "ok":
        open_rows, claimed, blocked = _health_rows(queue.get("open")), _health_rows(queue.get("claimed")), _health_rows(queue.get("blocked"))
        dead = [c for c in claimed if isinstance(c.get("heartbeat_age_seconds"), (int, float))
                and c["heartbeat_age_seconds"] >= lease_seconds]
        for claim in dead:
            item = str(claim.get("id", "?"))
            flags.append(_health_flag("dead_claim", RED, f"{item}: claim with no heartbeat for "
                               f"{sv.age(float(claim['heartbeat_age_seconds']))}",
                               f"held by {claim.get('box', '?')}/{claim.get('session', '?')}; a dead session "
                               "holds the row and no keeper can take it",
                               "Release the claim from an integrator box, then the next keeper takes it",
                               f"POST /items/{item}/release (an integrator box's token)", box=str(claim.get("box", ""))))
        if not open_rows and blocked:
            first = blocked[0]
            waits = ", ".join(str(w) for w in first.get("blocked_on", []) if w) or "the owner"
            flags.append(_health_flag("queue_blocked", AMBER, f"nothing claimable: {len(blocked)} row(s) blocked",
                               f"{first.get('id', '?')} waits on {waits}: {first.get('reason', '')}",
                               "Land or raise what the blocked rows wait on; a block naming no row is the owner's",
                               f"python3 -m scripts.todo_service.admin reprioritize {first.get('id', '?')} 1"))
        if slot_models:
            known = {m.strip().lower() for m in slot_models if m.strip()}
            untakeable = [r for r in open_rows if str(r.get("model") or "").lower() not in known]
            if untakeable and len(untakeable) == len(open_rows):
                models = sorted({str(r.get("model") or "(none)") for r in untakeable})
                flags.append(_health_flag("no_slot_model", AMBER,
                                   f"{len(untakeable)} open row(s) no keeper slot can take",
                                   f"their model is {', '.join(models)}; the slots run {', '.join(sorted(known))}",
                                   "Widen the keeper's model map, or change the rows' model (an integrator box)",
                                   f"PATCH /items/{untakeable[0].get('id', '?')} {{\"model\": \"sonnet\"}}"))
    elif queue.get("state") == "no_answer" and not _health_rows(todo_warnings):
        flags.append(_health_flag("queue_unread", AMBER, "the queue could not be read", str(queue.get("why", "")),
                           f"On the service's box: `systemctl --user status {TODO_SERVICE_UNIT}`"))

    integration = _health_map(sections.get("integration"))
    if integration.get("state") == "ok":
        scanned = integration.get("scanned_at")
        scanned_epoch = sv.iso_epoch(scanned) if isinstance(scanned, str) else None
        if scanned is None or scanned_epoch is None:
            flags.append(_health_flag("sync_stale", AMBER, "no seam scan on record",
                               "the todo service has never scanned the seams, so nothing is ready to merge",
                               f"On jeltz: `systemctl --user status {TODO_SYNC_UNIT}` and start it",
                               f"systemctl --user start {TODO_SYNC_UNIT}"))
        elif now - scanned_epoch > sync_stale_seconds:
            flags.append(_health_flag("sync_stale", AMBER, f"seam scan is {sv.age(now - scanned_epoch)} old",
                               "the integration board reads a stale scan: a pushed seam is not seen as ready",
                               "On jeltz: `journalctl --user -u skykeep-todo-sync.service -n 10`; restart the timer",
                               f"systemctl --user restart {TODO_SYNC_UNIT}"))

    fleet = _health_rows(sections.get("fleet"))
    memory = _health_map(sections.get("memory"))
    for row in _fleet_idle_boxes(fleet):
        box = _fleet_box_name(row)
        here = "(this box)" in str(row.get("box", ""))
        free = ""
        if here and isinstance(memory.get("available_gib"), (int, float)):
            if memory.get("below_floor"):
                continue                          # under its reserve: the keeper correctly starts nothing
            free = f" with {memory['available_gib']:g} GiB free"
        hold = str(row.get("keeper_hold_back") or "unknown")
        idle_minutes = row.get("idle_with_free_memory", "")
        idle = f"; {idle_minutes} idle with free memory" if isinstance(idle_minutes, str) and "min / 24 h" in idle_minutes else ""
        flags.append(_health_flag("box_idle", AMBER, f"{box} runs no agent{free}{idle}",
                           f"keeper holds back: {hold}{idle}",
                           f"On {box}: read the keeper's journal; restart it if it is stopped",
                           f"ssh {box} journalctl --user -u {TODO_KEEPER_UNIT} -n 30", box=box))
    for row in fleet:
        if _fleet_count(row.get("claude")) is None and row.get("note"):
            box = _fleet_box_name(row)
            flags.append(_health_flag("box_unread", AMBER, f"{box} not read", str(row.get("note")),
                               f"From this box: `ssh {box} true`; the probe needs a key the agent can use",
                               f"ssh -o BatchMode=yes {box} true", box=box))

    warnings = _health_map(sections.get("run_warnings"))
    for warning in _health_rows(warnings.get("warnings")):
        code = str(warning.get("what") or "").lower()
        if warning.get("state") == "ACTIVE" and any(p in code for p in PREFLIGHT_CODES):
            flags.append(_health_flag("preflight", RED, f"preflight red: {warning.get('subject') or code}",
                               str(warning.get("warning", "")),
                               "The seam is refused before any lane runs: fix the dependency or advisory it names"))

    run = _health_map(sections.get("integrator_run"))
    gate = _health_map(run.get("gate"))
    waiting = _health_rows(_health_map(run.get("next_batch")).get("waiting"))
    if waiting and gate.get("state") != "running":
        flags.append(_health_flag("batch_idle", AMBER, f"{len(waiting)} seam(s) pushed and waiting, no batch running",
                           "; ".join(f"{w.get('seam', '?')} ({w.get('why', 'ready')})" for w in waiting[:3]),
                           "Wake the integrator: it merges the ready seams and starts the gate",
                           "python3 scripts/agents/skybus/lease.py holder"))
    stage = str(run.get("stage") or "")
    if "waiting for lanes" in stage.lower() or gate.get("state") == "waiting":
        flags.append(_health_flag("gate_waiting", AMBER, "the gate waits for lanes", stage or "no lane has started",
                           "Check the gate boxes' lane stacks and memory; a lane that cannot start holds the batch"))

    for row in _health_rows(sections.get("usage")):
        pct, stop = row.get("pct"), row.get("stop")
        account = str(row.get("account", "default"))
        label = "Claude usage" + ("" if account == "default" else f" ({account})")
        if row.get("state") == sv.accounts.AT_STOP:
            flags.append(_health_flag("usage_stop", RED, f"{label} at the weekly stop",
                               f"{pct:g} % of the {stop:g} % stop line" if isinstance(pct, (int, float))
                               and isinstance(stop, (int, float)) else "at or past the stop line",
                               "Nothing new starts until the weekly reset; spend what is left on merges"))
        elif isinstance(pct, (int, float)) and isinstance(stop, (int, float)) and pct >= stop - usage_warn_margin_pct:
            flags.append(_health_flag("usage_near_stop", AMBER, f"{label} near the weekly stop",
                               f"{pct:g} % of the {stop:g} % stop line",
                               "Hand the queue to another engine before the stop; start no new row past 97 %"))

    if memory.get("below_floor"):
        flags.append(_health_flag("memory", AMBER, "this box is under its memory floor",
                           f"{memory.get('available', '?')} available; the loops wait below "
                           f"{memory.get('floor_gib', '?')} GiB ({memory.get('floor_source', '')})",
                           "Close what is not working, or let the loops wait; never lower the floor to fit"))
    disk = _health_map(sections.get("disk"))
    if disk.get("over_line"):
        flags.append(_health_flag("disk", RED, "the pool is past its line",
                           f"{disk.get('pct', '?')} % full, line {disk.get('line_pct', '?')} %",
                           "Preview a cleanup on the Lanes and gates page and delete what it names"))

    order = {RED: 0, AMBER: 1}
    flags.sort(key=lambda f: order.get(f["level"], 2))
    return flags


def health_strip(flags: Sequence[Mapping]) -> dict:
    """The strip: one colour and one headline for every page."""
    reds = [f for f in flags if f.get("level") == RED]
    ambers = [f for f in flags if f.get("level") == AMBER]
    if not reds and not ambers:
        return {"state": GREEN, "headline": ALL_CLEAR, "count": 0}
    shown = (reds or ambers)[:3]
    more = len(flags) - len(shown)
    headline = "; ".join(str(f.get("headline", "")) for f in shown) + (f" (+{more} more)" if more > 0 else "")
    return {"state": RED if reds else AMBER, "headline": headline, "count": len(flags)}


def health_section(sections: Mapping, todo_warnings: object, lease: Mapping | None, now: float, **limits) -> dict:
    """The Health section: the strip and its flags, from the other sections of the same state."""
    flags = health_flags(sections, todo_warnings, lease, now, **limits)
    strip = health_strip(flags)
    landing = _health_map(sections.get("landing"))
    landed = landing.get("landed_today")
    if landing.get("state") == "ok" and type(landed) is int and landed >= 0:
        strip["headline"] = f"{strip['headline']}; {landed} landed today"
    return {**strip, "flags": flags}
