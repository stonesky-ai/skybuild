"""Barriers: Claude usage, throughput, and every reason the build is not moving, by severity.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .. import hub as sv
from ..sources.lease import run_cmd

if TYPE_CHECKING:      # names used in annotations only (strings at runtime)
    from ..sections.work import Runner
    from ..settings import WebConfig
    from ..sources.loops_memory import LoopUnit


def _first_number(values: list[str]) -> float | None:
    for value in values:
        try:
            return float(value)
        except ValueError:
            continue
    return None


def claude_usage(wcfg: WebConfig, units: list[LoopUnit], now: float) -> list[dict]:
    """Each Claude account's weekly percent, read as claude_accounts.py reads it.

    Which files, the stop line and the staleness bound come from the
    environment (the same variables claude_accounts.py reads), else from the
    loop units' own command lines, which already name all three. Without a
    staleness bound a file cannot be judged fresh, so it is unknown.
    """
    stop = wcfg.weekly_stop_pct if wcfg.weekly_stop_pct is not None else \
        _first_number(sv.unit_flag(units, "--weekly-stop-pct"))
    max_age = wcfg.usage_max_age_minutes if wcfg.usage_max_age_minutes is not None else \
        _first_number(sv.unit_flag(units, "--usage-max-age-minutes"))
    if wcfg.claude_accounts is not None:
        try:
            found = sv.accounts.read_accounts(wcfg.claude_accounts)
        except sv.accounts.Refused as exc:
            return [{"account": "?", "state": sv.accounts.UNKNOWN, "pct": None, "stop": stop,
                     "reason": sv._shown(str(exc))}]
    else:
        files = [wcfg.claude_usage] if wcfg.claude_usage else \
            list(dict.fromkeys(Path(v).expanduser() for v in sv.unit_flag(units, "--claude-usage")))
        found = [sv.accounts.Account("default" if i == 0 else f"file {i + 1}", Path("/"), path)
                 for i, path in enumerate(files)]
    rows = []
    for account in found:
        if max_age is None:
            rows.append({"account": account.name, "state": sv.accounts.UNKNOWN, "pct": None, "stop": stop,
                         "reason": "no staleness bound configured, so the file cannot be judged fresh"})
            continue
        reading = sv.accounts.assess(account, now=now, stop_pct=stop if stop is not None else float("inf"),
                                  max_age_minutes=max_age)
        pct = reading.seven_day_pct if reading.state != sv.accounts.UNKNOWN else None
        rows.append({"account": account.name, "state": reading.state, "pct": pct, "stop": stop,
                     "reason": sv._shown(reading.reason)})
    return rows


def read_throughput(repo: Path, now: float, runner: Runner = run_cmd, days: int = 8) -> dict:
    """Commits (every ref, no merges) and landings (merges on HEAD's first parent) per 24 h.

    Bucket 0 is the last 24 hours; the best of the buckets before it is the
    week's maximum the page compares against.
    """
    since = f"--since=@{int(now - days * 86400)}"
    rc_c, commits = runner(["git", "log", "--all", "--no-merges", since, "--format=%ct"], cwd=repo,
                           timeout=30.0)
    rc_m, merges = runner(["git", "log", "HEAD", "--merges", "--first-parent", since, "--format=%ct"],
                          cwd=repo, timeout=30.0)
    if rc_c != 0 or rc_m != 0:
        return {"error": "the git history could not be read"}

    def buckets(text: str) -> list[int]:
        counts = [0] * days
        for line in text.split():
            try:
                back = int((now - int(line)) // 86400)
            except ValueError:
                continue
            if 0 <= back < days:
                counts[back] += 1
        return counts

    c, m = buckets(commits), buckets(merges)
    return {"error": "", "commits_24h": c[0], "commits_max": max(c[1:]), "merges_24h": m[0],
            "merges_max": max(m[1:]), "commits_days": c, "merges_days": m}


BAD, WARN, UNKNOWN, OK, INFO = "bad", "warn", "unknown", "ok", "info"
_SEVERITY = {BAD: 0, WARN: 1, UNKNOWN: 2, OK: 3, INFO: 4}


def _barrier(key: str, label: str, state: str, value: str, detail: str = "") -> dict:
    return {"key": key, "label": label, "state": state, "value": value, "detail": detail}


def assemble_barriers(wcfg: WebConfig, sections: dict, usage: list[dict], throughput: dict) -> dict:
    """What limits the build right now, worst first, each with the number behind it."""
    items: list[dict] = []
    if not usage:
        items.append(_barrier("claude", "Claude weekly usage", UNKNOWN, "unknown",
                              "no usage file configured, and no loop names one"))
    for row in usage:
        label = "Claude weekly usage" + ("" if row["account"] == "default" else f" ({row['account']})")
        stop = f"stop line {row['stop']:g} %" if row["stop"] is not None else "no stop line configured"
        if row["state"] == sv.accounts.UNKNOWN:
            items.append(_barrier("claude", label, UNKNOWN, "unknown", row["reason"]))
        elif row["state"] == sv.accounts.AT_STOP:
            items.append(_barrier("claude", label, BAD, f"{row['pct']:g} %", f"at or past the {stop}"))
        else:
            items.append(_barrier("claude", label, OK, f"{row['pct']:g} %", stop))

    memory = sections["memory"]
    if memory["available_gib"] is None or memory["floor_gib"] is None:
        items.append(_barrier("memory", "Free memory", UNKNOWN, f"{memory['available']} available",
                              f"floor {memory['floor_source']}"))
    else:
        state = BAD if memory["below_floor"] else OK
        items.append(_barrier("memory", "Free memory", state, f"{memory['available_gib']:g} GiB available",
                              f"loops wait below {memory['floor_gib']:g} GiB ({memory['floor_source']})"))

    for loop in sections["loops"]:
        waits = []
        if loop["memory_waits"]:
            waits.append(f"waited for memory {loop['memory_waits']}x")
        if loop["usage_waits"]:
            waits.append(f"waited for a usage window {loop['usage_waits']}x")
        recent = f" in the last {wcfg.journal_minutes:g} min" if waits else ""
        if loop["active"] == "failed":
            state = BAD
        elif loop["active"] == "unknown":
            state = UNKNOWN
        elif loop["active"] != "active" or waits:
            state = WARN
        else:
            state = OK
        items.append(_barrier("loop", loop["name"], state, loop["active"], "; ".join(waits) + recent))

    disk = sections["disk"]
    if disk.get("error"):
        items.append(_barrier("disk", "Disk", UNKNOWN, "unreadable", disk["error"]))
    else:
        items.append(_barrier("disk", "Disk", BAD if disk["over_line"] else OK, f"{disk['pct']:g} % full",
                              f"line {disk['line_pct']:g} % · {disk['source']}"))
    pred = disk.get("prediction", {})
    if pred.get("state") == "growing":
        warn_s = wcfg.runout_warn_hours * 3600
        state = BAD if pred["full_in_seconds"] < warn_s else WARN if pred["line_in_seconds"] < warn_s else OK
        items.append(_barrier("runout", "Disk run-out", state, f"full in {pred['full_in']}",
                              f"line in {pred['line_in']} at {pred['rate_text']}"))
    elif pred.get("state") == "steady":
        items.append(_barrier("runout", "Disk run-out", OK, "not filling", pred.get("rate_text", "")))
    else:
        items.append(_barrier("runout", "Disk run-out", UNKNOWN, pred.get("state", "unknown"),
                              f"{pred.get('samples', 0)} sample(s) over {pred.get('span', '0s')}; "
                              f"needs {pred.get('needs', '?')}"))

    seams = sections["seams"]
    ready = seams["ready"]
    if ready:
        oldest = seams["oldest_ready_seconds"] or 0.0
        state = WARN if oldest > wcfg.ready_wait_minutes * 60 else OK
        items.append(_barrier("landing", "Seams waiting to land", state, f"{len(ready)} READY",
                              f"oldest waited {sv.age(oldest)} (land by {sv.age(wcfg.ready_wait_minutes * 60)})"))
    else:
        items.append(_barrier("landing", "Seams waiting to land", OK, "none READY"))
    if seams["stuck"]:
        halted = sections["work"]["halted"]
        who = sorted({row["next"] for row in halted if row.get("next")})
        items.append(_barrier("stuck", "Stuck seams", WARN, f"{seams['stuck']} STUCK",
                              "next: " + ", ".join(who) if who else ""))

    if throughput.get("error"):
        items.append(_barrier("throughput", "Throughput (24 h)", UNKNOWN, "unreadable", throughput["error"]))
    else:
        flat = ready and throughput["merges_24h"] == 0
        items.append(_barrier(
            "throughput", "Throughput (24 h)", WARN if flat else INFO,
            f"{throughput['commits_24h']} commits · {throughput['merges_24h']} merges",
            f"best day this week: {throughput['commits_max']} commits · {throughput['merges_max']} merges"
            + ("; seams are READY and nothing landed" if flat else "")))

    unresolved = sections["unresolved"]
    count = unresolved.get("count")
    items.append(_barrier("unresolved", "Unresolved questions", UNKNOWN if count is None else INFO,
                          "unreadable" if count is None else str(count), "the owner's to answer"))

    items.sort(key=lambda b: _SEVERITY[b["state"]])
    limiting = [f"{b['label']}: {b['value']}" + (f" — {b['detail']}" if b["detail"] else "")
                for b in items if b["state"] in (BAD, WARN)]
    return {"items": items, "limiting": limiting}


def loop_facts(unit: LoopUnit) -> dict:
    return {"name": sv._shown(unit.name), "vendor": sv.vendor_of(unit.name), "active": unit.active,
            "sub": unit.sub, "main_pid": unit.main_pid, "memory_waits": unit.memory_waits,
            "last_memory_wait": unit.last_memory_wait, "usage_waits": unit.usage_waits}
