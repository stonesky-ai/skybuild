"""Live Claude sessions and their agents: `/proc` scans, the session id of a process, and the
transcript scanner that reads each agent's last stop reason.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .. import hub as sv

if TYPE_CHECKING:      # names used in annotations only (strings at runtime)
    from ..settings import Config


def iter_processes():
    """(pid, cmdline) for every process this user can read. /proc, no pgrep."""
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if not raw:
            continue
        yield int(entry.name), raw.replace(b"\0", b" ").decode("utf-8", "replace")


# ---------------------------------------------------------------- sessions + agents

@dataclass
class Session:
    pid: int
    session_id: str | None
    started: float


@dataclass
class AgentRow:
    agent_id: str
    model: str
    agent_type: str
    description: str
    worktree: str | None
    last_write: float
    state: str          # running | stale | done
    session_pid: int


def live_sessions(cfg: Config) -> list[Session]:
    """Every live Claude Code session, mapped to the session id it is writing.

    Two traps, both hit in testing: a session forks helpers (ugrep, ripgrep)
    that keep the claude binary as their /proc/<pid>/exe AND inherit its open
    descriptors, so an exe check alone counts one session several times —
    argv[0] is what separates a session from its helper. And a session that
    re-executes itself can appear twice under one id, so ids are deduplicated,
    the oldest process winning as the owner.
    """
    found: dict[str, Session] = {}
    unidentified: list[Session] = []
    for pid, cmd in sv.iter_processes():
        try:
            target = os.readlink(f"/proc/{pid}/exe")
        except OSError:
            continue
        if Path(target).name != "claude":
            continue
        argv0 = cmd.split(" ", 1)[0]
        if Path(argv0).name != "claude":
            continue                                   # a forked helper, not a session
        sess = Session(pid, _session_id_for(pid, cfg), _proc_start(pid))
        if sess.session_id is None:
            unidentified.append(sess)
            continue
        prior = found.get(sess.session_id)
        if prior is None or sess.started < prior.started:
            found[sess.session_id] = sess
    return sorted(found.values(), key=lambda s: s.started) + unidentified


_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _session_id_for(pid: int, cfg: Config) -> str | None:
    # A running session keeps an open descriptor on its own scratch tasks dir:
    # .../<project slug>/<session uuid>/tasks — the most reliable pid -> session link.
    fd_dir = Path(f"/proc/{pid}/fd")
    try:
        fds = list(fd_dir.iterdir())
    except OSError:
        fds = []
    for fd in fds:
        try:
            target = os.readlink(fd)
        except OSError:
            continue
        if cfg.projects_dir.name not in target:
            continue
        m = _UUID.search(target)
        if m:
            return m.group(0)
    # Fallback: the id is on the command line of a resumed session.
    try:
        cmd = (Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
               .decode("utf-8", "replace"))
    except OSError:
        return None
    m = re.search(r"--resume[= ]\s*(" + _UUID.pattern + ")", cmd)
    return m.group(1) if m else None


_CLK_TCK = os.sysconf("SC_CLK_TCK")


def _boot_time() -> float:
    try:
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except OSError:
        pass
    return 0.0


_BOOT = _boot_time()


def _stat_fields(pid: int) -> list[str] | None:
    """/proc/<pid>/stat past the comm field, so a command with spaces is safe."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    _, _, rest = raw.partition(") ")
    return rest.split() if rest else None


def _proc_start(pid: int) -> float:
    """True process start (field 22 against the kernel's boot time)."""
    fields = _stat_fields(pid)
    if fields and _BOOT:
        try:
            return _BOOT + int(fields[19]) / _CLK_TCK
        except (IndexError, ValueError):
            pass
    try:
        return Path(f"/proc/{pid}").stat().st_mtime
    except OSError:
        return time.time()


def _proc_cpu_seconds(pid: int) -> float | None:
    """utime + stime, for measuring whether something is actually working."""
    fields = _stat_fields(pid)
    if not fields:
        return None
    try:
        return (int(fields[11]) + int(fields[12])) / _CLK_TCK
    except (IndexError, ValueError):
        return None


class TranscriptScanner:
    """Incrementally tails session transcripts for agent-completion signals.

    Transcripts run to megabytes, so each refresh reads only what was appended
    since the last one. Two different signals matter, because the two kinds of
    agent report differently:

      background agents return their tool_result the instant they launch, so
      only a <task-notification> (or a task_status attachment) marks the end;
      foreground agents have no notification — their tool_result IS the end.
    """

    _TASK_NOTE = re.compile(r"<task-id>([^<]+)</task-id>.*?<status>([^<]+)</status>", re.DOTALL)
    _TASK_STATUS = re.compile(r'"taskId"\s*:\s*"([^"]+)"(.{0,600}?)"status"\s*:\s*"([^"]+)"', re.DOTALL)
    _TOOL_RESULT = re.compile(r'"tool_use_id"\s*:\s*"([^"]+)"')

    def __init__(self) -> None:
        self._offsets: dict[Path, tuple[int, int]] = {}   # path -> (inode, offset)
        self.finished: dict[str, str] = {}                 # agent id -> terminal status
        self.tool_results: set[str] = set()

    def update(self, path: Path) -> None:
        try:
            st = path.stat()
        except OSError:
            return
        inode, offset = self._offsets.get(path, (st.st_ino, 0))
        if inode != st.st_ino or st.st_size < offset:   # rotated or truncated
            offset = 0
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                fh.seek(offset)
                for line in fh:
                    self._scan(line)
                self._offsets[path] = (st.st_ino, fh.tell())
        except OSError:
            return

    def _scan(self, line: str) -> None:
        if "task-notification" in line:
            for task_id, status in self._TASK_NOTE.findall(line):
                status = status.strip()
                if status and status != "running":
                    self.finished[task_id.strip()] = status
        if "task_status" in line:
            for task_id, _mid, status in self._TASK_STATUS.findall(line):
                if status in ("completed", "failed", "killed", "cancelled"):
                    self.finished[task_id] = status
        if "tool_use_id" in line:
            self.tool_results.update(self._TOOL_RESULT.findall(line))


def last_stop_reason(path: Path, window: int = 262144) -> str | None:
    """`stop_reason` of the last complete message in an agent transcript.

    An agent that has delivered its final report ends on `end_turn`; one still
    working ends on `tool_use`. This is the fastest completion signal there is
    — the parent session's notification can lag it by a turn — so it is read
    straight from the tail rather than by parsing the whole file.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            fh.seek(max(0, size - window))
            chunk = fh.read()
    except OSError:
        return None
    lines = [ln for ln in chunk.split(b"\n") if ln.strip()]
    if not lines:
        return None
    if size > window and len(lines) < 2:
        return None                                    # only a partial line in view
    for raw in reversed(lines if size <= window else lines[1:]):
        try:
            message = json.loads(raw).get("message") or {}
        except ValueError:
            continue
        return message.get("stop_reason")
    return None


def read_agents(cfg: Config, sessions: list[Session], scanner: TranscriptScanner,
                now: float) -> list[AgentRow]:
    rows: list[AgentRow] = []
    stale_after = cfg.stale_minutes * 60
    for sess in sessions:
        if not sess.session_id:
            continue
        sub = cfg.projects_dir / sess.session_id / "subagents"
        if not sub.is_dir():
            continue
        scanner.update(cfg.projects_dir / f"{sess.session_id}.jsonl")
        for meta_path in sorted(sub.glob("agent-*.meta.json")):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8", errors="replace"))
            except (OSError, ValueError):
                continue
            agent_id = meta_path.name[len("agent-"):-len(".meta.json")]
            transcript = sub / f"agent-{agent_id}.jsonl"
            try:
                last_write = transcript.stat().st_mtime
            except OSError:
                last_write = meta_path.stat().st_mtime

            background = meta.get("requestShape") == "background"
            if background:
                done = agent_id in scanner.finished
            else:
                done = meta.get("toolUseId") in scanner.tool_results
            if not done:
                done = last_stop_reason(transcript) == "end_turn"
            if done:
                state = "done"
            elif now - last_write > stale_after:
                state = "stale"
            else:
                state = "running"

            worktree = meta.get("worktreePath")
            rows.append(AgentRow(
                agent_id=agent_id,
                model=str(meta.get("model") or "?"),
                agent_type=str(meta.get("agentType") or "?"),
                description=str(meta.get("description") or "(no description)"),
                worktree=Path(worktree).name if worktree else None,
                last_write=last_write,
                state=state,
                session_pid=sess.pid,
            ))
    rows.sort(key=lambda r: (r.state != "running", r.state != "stale", -r.last_write))
    return rows
