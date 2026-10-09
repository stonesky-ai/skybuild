"""The build line: where work stands between a filed row and a landed one, and what each box holds.

Pure functions over the page's own sections and the health flags built from them (owner interview
2026-10-06: "no single picture shows where work piles up or which box idles"). The line has six
stages in the order a row travels them: open, claimed, pushed, ready, gate, landed. Each stage
carries its count, two or three short facts, and the level of the worst health flag that belongs to
it; the pile-up is the first flagged stage, with that flag's cause and fix. Under the line, one tile
per box the page has heard of: the rows it holds, the agents it runs, and why it idles.

Nothing here reads a file, a process or the network, and no threshold is decided here: a stage is
amber or red only because `sources/health.py` raised a flag for it. A section that was not read
leaves its stage saying so, and the rest of the line still comes back.
"""
from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence

from .. import hub as sv

#: The stages in travel order: key, the name shown, and the view that holds the detail.
STAGES: tuple[tuple[str, str, str], ...] = (
    ("open", "Open", "queue"),
    ("claimed", "Claimed", "queue"),
    ("pushed", "Pushed", "integration"),
    ("ready", "Ready", "integration"),
    ("gate", "Gate", "lanes"),
    ("landed", "Landed today", "usage"),
)
#: Which stage a health flag belongs to (`sources/health.py` names the keys).
FLAG_STAGE: dict[str, str] = {
    "queue_blocked": "open", "no_slot_model": "open", "queue_unread": "open",
    "dead_claim": "claimed",
    "sync_stale": "pushed",
    "batch_idle": "ready",
    "integrator": "gate", "gate_waiting": "gate", "preflight": "gate", "disk": "gate",
}
#: Flags that belong to a box rather than to a stage.
BOX_FLAGS: tuple[str, ...] = ("box_idle", "box_unread", "memory", "dead_claim")
NOT_READ = "not read"
_LEVEL_ORDER = {"red": 0, "amber": 1}
_SHA = re.compile(r"\s+(?:at\s+)?[0-9a-f]{7,40}\b")
#: How many reasons, models and boxes a stage names before it says "and N more".
FACTS_SHOWN = 3


def _rows(value: object) -> list[dict]:
    return [dict(row) for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []


def _map(value: object) -> dict:
    return dict(value) if isinstance(value, Mapping) else {}


def _flags(value: object) -> list[dict]:
    """The health flags as dicts; anything that is not a list of them is no flag, never a failed page."""
    return _rows(list(value)) if isinstance(value, (list, tuple)) else []


def _tally(words: Sequence[str]) -> str:
    """"sonnet 40, opus 12, fable 3": the commonest first, the tail counted rather than listed."""
    counted = Counter(words).most_common()
    shown = ", ".join(f"{word} {count}" for word, count in counted[:FACTS_SHOWN])
    more = len(counted) - FACTS_SHOWN
    return shown + (f" and {more} more" if more > 0 else "")


def _reason_head(reason: object) -> str:
    """A seam's first reason without the sha or path that makes it unique, so equal causes count together."""
    text = _SHA.sub("", str(reason)).split(":", 1)[0].split("(", 1)[0].strip()
    return text[:60] or "no reason given"


def _pushed_at(seam: Mapping) -> float | None:
    """When the seam was pushed, from its timeline; None when the service recorded no push."""
    stamps = [sv.iso_epoch(step["at"]) for step in _rows(seam.get("timeline"))
              if step.get("step") == "push" and isinstance(step.get("at"), str)]
    known = [stamp for stamp in stamps if stamp is not None]
    return max(known) if known else None


def _oldest(seams: Sequence[Mapping], now: float) -> str:
    stamps = [stamp for stamp in (_pushed_at(seam) for seam in seams) if stamp is not None]
    return f"oldest pushed {sv.age(now - min(stamps))} ago" if stamps else ""


def _box_key(name: object) -> str:
    """One name per box whatever wrote it: the fleet says "Jeltz (this box)", the todo service "jeltz"."""
    return str(name).split(" ", 1)[0].strip().lower()


def _stage(key: str, count: int | None, facts: Sequence[str], *, word: str = "") -> dict:
    title, view = next((title, view) for stage, title, view in STAGES if stage == key)
    return {"key": key, "title": title, "view": view, "count": count, "word": word,
            "facts": [fact for fact in facts if fact], "level": "", "flag": ""}


def flow_stages(sections: Mapping, now: float, *, lease_seconds: float) -> list[dict]:
    """The six stages with their counts and facts; no levels yet (`flow_section` lays the flags on)."""
    queue = _map(sections.get("queue"))
    if queue.get("state") == "ok":
        open_rows, claimed, blocked = _rows(queue.get("open")), _rows(queue.get("claimed")), _rows(queue.get("blocked"))
        stale = [c for c in claimed if isinstance(c.get("heartbeat_age_seconds"), (int, float))
                 and c["heartbeat_age_seconds"] >= lease_seconds]
        stages = [
            _stage("open", len(open_rows), [
                _tally([str(row.get("model") or "no model") for row in open_rows]),
                f"{len(blocked)} blocked" if blocked else "nothing blocked"]),
            _stage("claimed", len(claimed), [
                _tally([str(row.get("box") or "?") for row in claimed]),
                f"{len(stale)} with no heartbeat" if stale else ("every heartbeat alive" if claimed else "")]),
        ]
    else:
        why = str(queue.get("why") or NOT_READ)
        stages = [_stage("open", None, [why]), _stage("claimed", None, [])]

    integration = _map(sections.get("integration"))
    if integration.get("state") == "ok":
        seams = [seam for group in _rows(integration.get("sets")) for seam in _rows(group.get("seams"))]
        ready = [seam for seam in seams if seam.get("ready") is True]
        waiting = [seam for seam in seams if seam.get("ready") is not True]
        reasons = [_reason_head(seam["reasons"][0]) for seam in waiting
                   if isinstance(seam.get("reasons"), list) and seam["reasons"]]
        stages += [
            _stage("pushed", len(waiting), [_oldest(waiting, now), _tally(reasons)]),
            _stage("ready", len(ready), [_oldest(ready, now),
                                         f"{sum(1 for seam in ready if seam.get('touches_migration'))} with a migration"
                                         if any(seam.get("touches_migration") for seam in ready) else ""]),
        ]
    else:
        why = str(integration.get("why") or NOT_READ)
        stages += [_stage("pushed", None, [why]), _stage("ready", None, [])]

    run = _map(sections.get("integrator_run"))
    gate = _map(run.get("gate"))
    state = str(gate.get("state") or "")
    running = state == "running"
    facts = [str(run.get("batch") or "") if running else "",
             f"running {gate.get('running_for')}" if running and gate.get("running_for") not in (None, "", "-") else "",
             f"ends about {gate.get('finishes_about')}" if running and gate.get("finishes_about") else "",
             "" if running else str(run.get("stage") or "")]
    word = "running" if running else "waiting" if state == "waiting" else "idle" if run else NOT_READ
    stages.append(_stage("gate", None, facts, word=word))

    landing = _map(sections.get("landing"))
    if landing.get("state") == "ok":
        days = [day for day in _rows(landing.get("days")) if type(day.get("landed")) is int]
        stamps = [stamp for stamp in (sv.iso_epoch(row["land_at"]) for row in _rows(landing.get("rows"))
                                      if isinstance(row.get("land_at"), str)) if stamp is not None]
        landed = landing.get("landed_today")
        stage = _stage("landed", landed if type(landed) is int else None, [
            f"last landed {sv.age(now - max(stamps))} ago" if stamps else "",
            f"{days[-2]['landed']} the day before" if len(days) >= 2 else ""])
        stage["days"] = [{"day": str(day.get("day", "")), "landed": day["landed"]} for day in days]
        stages.append(stage)
    else:
        stages.append(_stage("landed", None, [str(landing.get("why") or NOT_READ)]))
    return stages


def flow_boxes(sections: Mapping, flags: Sequence[Mapping], *, lease_seconds: float) -> list[dict]:
    """One tile per box the page has heard of: from the fleet rows, the claims held and the rows given back."""
    boxes: dict[str, dict] = {}

    def tile(name: object) -> dict:
        key = _box_key(name)
        return boxes.setdefault(key, {"box": key, "here": False, "read": False, "agents": None, "claims": [],
                                      "working_on": "", "keeper": "", "free": "", "idle": "", "gave_back": 0,
                                      "level": "", "note": ""})

    memory = _map(sections.get("memory"))
    for row in _rows(sections.get("fleet")):
        if _box_key(row.get("box")) in ("", "fleet"):
            continue                              # the fleet view's own failure row, not a box
        box = tile(row.get("box"))
        box["here"] = "(this box)" in str(row.get("box", ""))
        counts = {kind: sv._fleet_count(row.get(kind)) for kind in ("claude", "claude_headless", "codex", "grok")}
        box["read"] = all(count is not None for count in counts.values())
        box["agents"] = sum(counts.values()) if box["read"] else None
        box["working_on"] = str(row.get("working_on") or "") if box["read"] else ""
        box["keeper"] = str(row.get("keeper_hold_back") or "")
        idle = row.get("idle_with_free_memory")
        box["idle"] = idle if isinstance(idle, str) and "min / 24 h" in idle else ""
        if not box["read"]:
            box["note"] = str(row.get("note") or NOT_READ)
        if box["here"] and isinstance(memory.get("available"), str):
            box["free"] = memory["available"] + " free" + (", under its floor" if memory.get("below_floor") else "")

    queue = _map(sections.get("queue"))
    if queue.get("state") == "ok":
        for claim in _rows(queue.get("claimed")):
            beat = claim.get("heartbeat_age_seconds")
            tile(claim.get("box") or "?")["claims"].append({
                "id": str(claim.get("id", "?")), "session": str(claim.get("session") or ""),
                "model": str(claim.get("model") or ""),
                "heartbeat": sv.age(float(beat)) if isinstance(beat, (int, float)) else "",
                "stale": isinstance(beat, (int, float)) and beat >= lease_seconds})
        for row in _rows(queue.get("open")):
            for name, count in _map(row.get("give_backs_by_box")).items():
                if type(count) is int and count > 0:
                    tile(name)["gave_back"] += count

    for flag in _flags(flags):
        if flag.get("key") not in BOX_FLAGS:
            continue
        name = flag.get("box") or next((box["box"] for box in boxes.values() if box["here"]), "")
        if not name or _box_key(name) not in boxes:
            continue
        box = boxes[_box_key(name)]
        level = "red" if flag.get("level") == "red" else "amber"
        if _LEVEL_ORDER[level] < _LEVEL_ORDER.get(box["level"], 2):
            box["level"], box["note"] = level, str(flag.get("headline", ""))
    for box in boxes.values():
        if not box["level"]:
            busy = bool(box["claims"]) or bool(box["agents"])
            box["level"] = "ok" if busy else "idle" if box["read"] or queue.get("state") == "ok" else "unknown"
    return sorted(boxes.values(), key=lambda box: (not box["here"], box["box"]))


def flow_section(sections: Mapping, flags: Sequence[Mapping], now: float, *, lease_seconds: float) -> dict:
    """The Build line section: the stages, the pile-up (or None), and the box tiles."""
    stages = flow_stages(sections, now, lease_seconds=lease_seconds)
    by_key = {stage["key"]: stage for stage in stages}
    pile = None
    for flag in _flags(flags):                    # red first: `health_flags` sorted them
        key = FLAG_STAGE.get(str(flag.get("key")))
        if key is None or key not in by_key:
            continue
        stage = by_key[key]
        level = "red" if flag.get("level") == "red" else "amber"
        if not stage["level"]:
            stage["level"], stage["flag"] = level, str(flag.get("headline", ""))
        if pile is None:
            pile = {"stage": key, "title": stage["title"], "level": level,
                    "headline": str(flag.get("headline", "")), "cause": str(flag.get("cause", "")),
                    "fix": str(flag.get("fix", "")), "command": str(flag.get("command", ""))}
    for stage in stages:
        if not stage["level"]:
            stage["level"] = "unknown" if stage["count"] is None and stage["word"] in ("", NOT_READ) else "ok"
    return {"stages": stages, "pile": pile,
            "boxes": flow_boxes(sections, flags, lease_seconds=lease_seconds)}


def batch_now(sections: Mapping) -> dict:
    """The seams of the batch being integrated now: the inset at the top of the Integrator now box.

    Three sources, the surest first. The merges on the pushed trunk since the last ledger commit are the
    batch for certain. An integrator that merges in a scratch worktree shows none there until it pushes, so
    next comes the set the todo service marks running, with the seams that were ready when it was read.
    With neither, no batch is known: a running gate is said to be running with its seams not visible here,
    and the seams ready on the board are listed as what the next batch would take, never as this one.
    """
    run = _map(sections.get("integrator_run"))
    merged = _rows(run.get("seams_in_batch"))
    if merged:
        tree = _map(run.get("batch_tree"))
        return {"state": "merged", "headline": f"In this batch: {len(merged)} merged",
                "source": (f"merged in the integrator's tree {tree.get('name')} (branch {tree.get('branch')}); "
                           "pushed with the ledger commit" if tree
                           else "merged on the pushed trunk since the last ledger commit"),
                "seams": [{"seam": str(m.get("seam", "?")), "note": str(m.get("merged") or "")}
                          for m in merged]}
    integration = _map(sections.get("integration"))
    read = integration.get("state") == "ok"
    sets = _rows(integration.get("sets")) if read else []

    def ready(one: Mapping) -> list[dict]:
        ids = one.get("ready")
        return [{"seam": str(seam), "note": ""} for seam in ids] if isinstance(ids, list) else []

    running = next((one for one in sets if one.get("state") == "running"), None)
    if running is not None:
        seams = ready(running)
        who = f", started by {running['started_by']}" if running.get("started_by") else ""
        return {"state": "running", "headline": f"In this batch: {len(seams)} seam(s)",
                "source": f"the set \"{running.get('name') or running.get('id') or '?'}\" the todo service marks running{who}",
                "seams": seams}
    gate = _map(run.get("gate"))
    pending = next((one for one in sets if one.get("state") == "pending"), None)
    seams = ready(pending) if pending is not None else []
    if gate.get("state") == "running":
        headline = "A gate is running, and its seams are not visible here"
        source = ("no seam is merged on the pushed trunk since the last ledger commit and the todo service "
                  "marks no set running: the integrator merges in a scratch worktree until it pushes")
    else:
        headline = "No batch is being integrated now"
        source = "" if read else str(integration.get("why") or "the integration board was not read")
    return {"state": "next" if seams else "none", "headline": headline, "source": source,
            "next": f"Ready for the next batch: {len(seams)}" if seams else "", "seams": seams}

