"""Loops and memory: the loop units, MemAvailable and the memory floor, the process table.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .. import hub as sv
from ..sources.lease import run_cmd

if TYPE_CHECKING:      # names used in annotations only (strings at runtime)
    from ..sections.work import Runner


# ---------------------------------------------------------------- loops and memory

#: A loop's own log line when it waits for memory (codex_loop.py, grok_loop.py):
#: `MemAvailable 4.6 GiB < 6.0 GiB: waiting 5 min`.
MEMORY_WAIT = re.compile(r"MemAvailable ([\d.]+) GiB < ([\d.]+) GiB: waiting")
#: And when it sleeps for a usage window to reset.
USAGE_WAIT = re.compile(r"usage limit: sleeping")
_ARGV = re.compile(r"argv\[\]=(.*?) ;")
_PAGE = os.sysconf("SC_PAGE_SIZE")


@dataclass
class LoopUnit:
    name: str
    active: str                 # systemd's ActiveState; "unknown" when systemd did not answer
    sub: str = ""
    main_pid: int = 0
    argv: tuple[str, ...] = ()
    memory_waits: int = 0
    last_memory_wait: str = ""
    usage_waits: int = 0


def unit_argv(execstart: str) -> tuple[str, ...]:
    """The command line in `systemctl show -p ExecStart`'s `{ path=… ; argv[]=… ; … }`."""
    m = _ARGV.search(execstart)
    return tuple(m.group(1).split()) if m else ()


def list_loop_units(glob: str, runner: Runner = run_cmd) -> list[str]:
    rc, out = runner(["systemctl", "--user", "list-units", "--all", "--plain", "--no-legend", glob])
    if rc != 0:
        return []
    return sorted({line.split()[0] for line in out.splitlines() if line.split()})


def read_loop_unit(name: str, journal_minutes: float, runner: Runner = run_cmd) -> LoopUnit:
    """One unit's state, its command line, and how often it waited lately.

    Read only: `systemctl show` and `journalctl`, never a verb that changes a unit.
    """
    rc, out = runner(["systemctl", "--user", "show", name, "-p", "ActiveState", "-p", "SubState",
                      "-p", "MainPID", "-p", "ExecStart"])
    props: dict[str, str] = {}
    for line in out.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            props[key] = value
    unit = LoopUnit(name, props.get("ActiveState") or "unknown" if rc == 0 else "unknown",
                    props.get("SubState", ""))
    try:
        unit.main_pid = int(props.get("MainPID", "0") or 0)
    except ValueError:
        unit.main_pid = 0
    unit.argv = unit_argv(props.get("ExecStart", ""))
    rc, journal = runner(["journalctl", "--user", "-u", name, "--since", f"-{int(journal_minutes)}min",
                          "-o", "cat", "--no-pager"], timeout=15.0)
    if rc == 0:
        for line in journal.splitlines():
            if MEMORY_WAIT.search(line):
                unit.memory_waits += 1
                unit.last_memory_wait = sv._shown(line)
            elif USAGE_WAIT.search(line):
                unit.usage_waits += 1
    return unit


def unit_flag(units: list[LoopUnit], flag: str) -> list[str]:
    """Every value the units' command lines give `flag`, in unit order."""
    return [value for unit in units if (value := sv._flag(list(unit.argv), flag))]


def read_meminfo(path: Path = Path("/proc/meminfo")) -> dict[str, int]:
    """/proc/meminfo in bytes. Unreadable is empty, which reads as unknown."""
    out: dict[str, int] = {}
    try:
        text = path.read_text()
    except OSError:
        return out
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if not parts:
            continue
        try:
            value = int(parts[0])
        except ValueError:
            continue
        out[key.strip()] = value * 1024 if parts[1:2] == ["kB"] else value
    return out


class ProcTable:
    """Every process's parent and resident memory, read once per refresh."""

    def __init__(self, parent: dict[int, int], rss: dict[int, int]) -> None:
        self.parent = parent
        self.rss = rss
        self.children: dict[int, list[int]] = {}
        for pid, ppid in parent.items():
            self.children.setdefault(ppid, []).append(pid)

    @classmethod
    def read(cls) -> ProcTable:
        parent: dict[int, int] = {}
        rss: dict[int, int] = {}
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            fields = sv._stat_fields(int(entry.name))
            if not fields or len(fields) < 22:
                continue
            try:
                parent[int(entry.name)] = int(fields[1])
                rss[int(entry.name)] = int(fields[21]) * _PAGE
            except ValueError:
                continue
        return cls(parent, rss)

    def tree(self, root: int) -> set[int]:
        if root not in self.rss:
            return set()
        seen, todo = set(), [root]
        while todo:
            pid = todo.pop()
            if pid in seen:
                continue
            seen.add(pid)
            todo.extend(self.children.get(pid, ()))
        return seen


def memory_section(meminfo: dict[str, int], floor_gib: float | None, floor_source: str,
                   procs: ProcTable, roots: list[tuple[str, str, int, str]]) -> dict:
    """The machine's free memory against the loops' floor, and what each session holds.

    `roots` is (kind, name, pid, detail) per session, loop or worker; each is
    counted with every process under it, and a root already inside another's
    tree is not counted twice.
    """
    counted: set[int] = set()
    rows = []
    for kind, name, pid, detail in roots:
        if pid <= 0 or pid in counted:
            continue
        tree = procs.tree(pid) - counted
        if not tree:
            continue
        counted |= tree
        total = sum(procs.rss.get(p, 0) for p in tree)
        rows.append({"kind": kind, "name": sv._shown(name), "pid": pid, "procs": len(tree),
                     "rss": total, "rss_text": sv.human_bytes(total), "detail": sv._shown(detail)})
    rows.sort(key=lambda r: -r["rss"])
    available = meminfo.get("MemAvailable")
    gib = 1024 ** 3
    return {
        "total": sv.human_bytes(meminfo.get("MemTotal")),
        "available": sv.human_bytes(available),
        "available_gib": round(available / gib, 1) if available is not None else None,
        "floor_gib": floor_gib,
        "floor_source": floor_source,
        "below_floor": (available is not None and floor_gib is not None and available < floor_gib * gib),
        "swap_used": sv.human_bytes(meminfo["SwapTotal"] - meminfo["SwapFree"])
        if "SwapTotal" in meminfo and "SwapFree" in meminfo else "?",
        "sessions": rows,
    }


def memory_floor(configured: float | None, units: list[LoopUnit]) -> tuple[float | None, str]:
    """The floor under which the loops wait: configured, else the highest a loop names."""
    if configured is not None:
        return configured, f"{sv.ENV}MEM_FLOOR_GIB"
    floors = []
    for value in unit_flag(units, "--min-mem-gib"):
        try:
            floors.append(float(value))
        except ValueError:
            continue
    if floors:
        return max(floors), "the loops' --min-mem-gib"
    return None, "unset (no loop names one)"


def memory_roots(frame, units: list[LoopUnit], workers: list) -> list[tuple[str, str, int, str]]:
    """Every session, loop and worker whose memory the page shows."""
    running: dict[int, int] = {}
    for agent in frame.agents:
        if agent.state == "running":
            running[agent.session_pid] = running.get(agent.session_pid, 0) + 1
    roots: list[tuple[str, str, int, str]] = []
    for sess in frame.sessions:
        name = f"session {sess.session_id[:8]}" if sess.session_id else "session (unidentified)"
        detail = f"up {sv.age(frame.now - sess.started)}, {running.get(sess.pid, 0)} sub-agent(s) running"
        roots.append(("claude", name, sess.pid, detail))
    for unit in units:
        roots.append((sv.vendor_of(unit.name), unit.name, unit.main_pid, f"{unit.active} {unit.sub}".strip()))
    if frame.codex is not None:
        for pid in frame.codex.pids:
            roots.append(("codex", f"codex loop {frame.codex.stream or ''}".strip(), pid, frame.codex.phase))
    for worker in workers:
        roots.append(("grok", f"grok {worker.session_id[:8]}", worker.pid, worker.model))
    for pid in frame.wd.pids:
        roots.append(("watchdog", "watchdog", pid, ""))
    return roots
