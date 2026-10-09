"""Sub-agents by session: each session's helper agents, their heartbeats, and the loops' current step.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .. import hub as sv
from ..settings import ENV_PREFIX

if TYPE_CHECKING:      # names used in annotations only (strings at runtime)
    from ..settings import Config
    from ..sources.codex_loop import CodexLoop
    from ..sources.sessions import AgentRow, Session
    from ..sources.worktrees import WorktreeRow
    from ..terminal import Paint


# ---------------------------------------------------------------- sub-agents by session

# Each live session's own sub-agents, listed under it. The Agent tool cuts a
# worktree named `agent-<id>` for an isolated agent, and the session that
# spawned it keeps `subagents/agent-<id>.meta.json` naming that worktree —
# the file `read_agents` already reads — so that session owns it. The same
# files with no worktree are the session's in-process agents, and an agent
# CLI whose parent chain reaches the session (walked as `read_lanes` walks a
# lane) is its helper process. The unattended loops run outside every
# session, so each one's current step is read from its own state directory.
# Everything is read; a worktree is only listed, never touched.

#: A worktree cut for a sub-agent: `<repo>/.claude/worktrees/agent-<id>`.
AGENT_WORKTREE = re.compile(r"^agent-([A-Za-z0-9]+)$")
#: argv[0] of an agent CLI a session may run as a helper process.
HELPER_AGENTS = frozenset({"claude", "codex", "grok"})
#: The setting (or `--stream-state`) naming the loops' state directories.
STREAM_STATE_SETTING = ENV_PREFIX + "STREAM_STATE"
#: A helper's command line is shown only this far: a CLI's argv can run to kilobytes.
HELPER_COMMAND_CHARS = 160
AGENT_HEARTBEAT_DIR = "SKYKEEP_AGENT_HEARTBEAT_DIR"
HEARTBEAT_STATES = frozenset({"running", "idle", "done"})


@dataclass
class SubAgent:
    kind: str                   # worktree | in-process | process
    name: str                   # the worktree's directory, the agent's type, or the program
    state: str                  # running | quiet | done
    detail: str                 # branch and description, model and description, or pid and command
    since: float | None = None  # the agent's last write, or the helper process's start
    model: str = ""              # heartbeat-reported model; empty when unreported
    doing: str = ""              # heartbeat-reported work; untrusted text
    started_at: float | None = None


@dataclass
class SessionAgents:
    session: Session
    agents: list[SubAgent] = field(default_factory=list)


@dataclass
class LoopSteps:
    refused: str = ""                                      # why no loop was read; "" when named
    loops: list[CodexLoop] = field(default_factory=list)


def read_session_agents(cfg: Config, sessions: list[Session], agents: list[AgentRow],
                        worktrees: list[WorktreeRow]) -> list[SessionAgents]:
    """Every live session, each with its own sub-agents; a session with none has an empty list.

    `agents` and `worktrees` are `read_agents`' and `read_worktrees`' own
    answers, so this adds no second discovery of either. A worktree agent is
    listed while its worktree is (whatever its transcript says, since a
    finished agent's worktree still holds its work) and leaves with it; one
    whose session is not live is nobody's here, as a session's death takes its
    agents with it. An in-process agent is listed while running or quiet.
    """
    listed = {s.pid: SessionAgents(s) for s in sessions}
    heartbeat_root = os.environ.get(AGENT_HEARTBEAT_DIR, "")
    heartbeat_dir = Path(heartbeat_root) if heartbeat_root and Path(heartbeat_root).is_absolute() else None
    heartbeat_now = time.time()
    by_tree = {a.worktree: a for a in agents if a.worktree}
    by_id = {a.agent_id: a for a in agents}
    placed: set[str] = set()
    for tree in worktrees:
        name = tree.path.name
        m = AGENT_WORKTREE.match(name)
        agent = by_tree.get(name) or (by_id.get(m.group(1)) if m else None)
        if agent is None or agent.session_pid not in listed:
            continue
        placed.add(agent.agent_id)
        row = SubAgent("worktree", name, "unreported",
                       f"{tree.branch} · {agent.description}", agent.last_write)
        _read_agent_heartbeat(row, agent.agent_id, heartbeat_dir, heartbeat_now,
                              cfg.stale_minutes * 60)
        listed[agent.session_pid].agents.append(row)
    for agent in agents:
        if (agent.worktree or agent.agent_id in placed or agent.state == "done"
                or agent.session_pid not in listed):
            continue
        row = SubAgent("in-process", agent.agent_type, "unreported",
                       f"{agent.model} · {agent.description}", agent.last_write)
        _read_agent_heartbeat(row, agent.agent_id, heartbeat_dir, heartbeat_now,
                              cfg.stale_minutes * 60)
        listed[agent.session_pid].agents.append(row)
    owners = {pid: entry.session for pid, entry in listed.items()}
    for pid, owner, cmd in _helper_processes(cfg, owners):
        shown = sv._shown(cmd)
        if len(shown) > HELPER_COMMAND_CHARS:
            shown = shown[:HELPER_COMMAND_CHARS - 1] + "…"
        listed[owner].agents.append(SubAgent(
            "process", Path(cmd.split(" ", 1)[0]).name, "running", f"pid {pid} · {shown}",
            sv._proc_start(pid)))
    return list(listed.values())


def _read_agent_heartbeat(row: SubAgent, agent_id: str, directory: Path | None,
                          now: float, stale_after: float) -> None:
    """Apply only the beat naming this known agent; malformed beats report nothing."""
    if directory is None or not agent_id or Path(agent_id).name != agent_id:
        return
    try:
        beat = json.loads((directory / f"{agent_id}.json").read_text(encoding="utf-8"))
        if not isinstance(beat, dict) or beat.get("name") != agent_id:
            return
        model, state, doing = (beat.get(key) for key in ("model", "state", "doing"))
        if not all(isinstance(value, str) for value in (model, state, doing)):
            return
        if state not in HEARTBEAT_STATES:
            return
        started = datetime.fromisoformat(beat["started_at"])
        latest = datetime.fromisoformat(beat["beat_at"])
        if started.tzinfo is None or latest.tzinfo is None:
            return
        start_time, beat_time = started.timestamp(), latest.timestamp()
        if start_time > beat_time or beat_time > now:
            return
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        return
    row.state = state if now - beat_time <= stale_after else "stale"
    row.model = model
    row.doing = doing
    row.started_at = start_time
    row.since = beat_time


def _helper_processes(cfg: Config, sessions: dict[int, Session]) -> list[tuple[int, int, str]]:
    """(pid, owning session pid, command line) of each agent CLI a live session launched."""
    procs = dict(sv.iter_processes())
    parents = sv._parent_map(procs)
    found: list[tuple[int, int, str]] = []
    for pid, cmd in procs.items():
        program = Path(cmd.split(" ", 1)[0]).name
        if program not in HELPER_AGENTS:
            continue
        owner = sv._owning_session(parents.get(pid, 0), parents, sessions)
        if owner is None:
            continue
        own_id = sessions[owner].session_id
        if program == "claude" and own_id and sv._session_id_for(pid, cfg) == own_id:
            continue                          # the session re-executing itself, not a helper
        found.append((pid, owner, cmd))
    return sorted(found)


def _resolved(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except (OSError, RuntimeError):
        return path


def read_loop_steps(cfg: Config) -> LoopSteps:
    """Each named loop's current step from its own state directory, or a refusal naming the setting.

    Only the directories `--stream-state` / SESSIONVIEW_STREAM_STATE name are
    read. The Codex default the seams fall back to is nobody's choice for this
    panel, so with none named it refuses, naming the setting, rather than read
    a guessed path (ADR-0002). A loop is live while a process carries
    `--state-dir <that directory>`; with none, `apply_loop_log` reads it as
    stopped whatever its log last said.
    """
    if not (cfg.stream_state_named and cfg.stream_state):
        return LoopSteps(refused=f"{STREAM_STATE_SETTING} is not set (nor --stream-state), so no "
                                 "loop state directory is named and none is assumed (ADR-0002)")
    directories = list(dict.fromkeys(cfg.stream_state))
    wanted = {_resolved(d): d for d in directories}
    pids: dict[Path, list[int]] = {d: [] for d in directories}
    for pid, cmd in sv.iter_processes():
        value = sv._flag(cmd.split(), "--state-dir")
        directory = wanted.get(_resolved(Path(value))) if value else None
        if directory is not None:
            pids[directory].append(pid)
    loops: list[CodexLoop] = []
    for directory in directories:
        loop = sv.CodexLoop(directory, sorted(pids[directory]))
        try:
            sv.apply_loop_log(loop, (directory / "loop.log").read_text(encoding="utf-8", errors="replace"))
        except OSError:
            loop.note = "no loop.log in this state directory"
        if loop.phase == "step" and loop.step:
            loop.step.doing = sv.summarize_step(directory / loop.step.name)[0]
        loops.append(loop)
    return LoopSteps("", loops)


def render_session_agents(c: Paint, by_session: list[SessionAgents], loops: LoopSteps | None,
                          width: int, now: float) -> list[str]:
    """Each live session with its sub-agents under it, then each named loop's current step."""
    count = sum(len(entry.agents) for entry in by_session)
    sessions = f"{len(by_session)} session{'s' if len(by_session) != 1 else ''}"
    lines = [f"{c.bold('By session')} {count} sub-agent{'s' if count != 1 else ''} under {sessions}"]
    paint = {"running": c.green, "idle": c.dim, "stale": c.yellow,
             "unreported": c.yellow, "quiet": c.yellow}
    for entry in by_session:
        sess = entry.session
        name = f"session {sess.session_id[:8]}" if sess.session_id else "session (unidentified)"
        lines.append(sv.trim(f"   {c.cyan(name)} {c.dim(f'pid {sess.pid}')}", width))
        if not entry.agents:
            lines.append(c.dim("      (no sub-agents)"))
        for agent in entry.agents:
            state = paint.get(agent.state, c.dim)(agent.state)
            since = c.dim(f" · {sv.age(now - agent.since)}") if agent.since else ""
            heartbeat = (f" · {sv._shown(agent.model)} · {sv.age(now - agent.started_at)}"
                         f" · {sv._shown(agent.doing)}" if agent.started_at is not None else "")
            lines.append(sv.trim(f"      {agent.kind:<10} {sv._shown(agent.name)} {state} "
                              f"{sv._shown(agent.detail)}{heartbeat}{since}", width))
    if loops is not None:
        if loops.refused:
            lines.append(sv.trim("   " + c.yellow("loops refused: ") + loops.refused, width))
        for loop in loops.loops:
            stepping = loop.phase == "step" and loop.step is not None
            since = f" {sv.age(now - loop.since)}" if loop.since else ""
            step = f" {loop.step.name}" if stepping else ""
            doing = sv._shown(loop.step.doing if stepping else loop.note)
            lines.append(sv.trim(f"   loop {c.cyan(loop.stream or '?')} "
                              f"{sv._PHASE_TEXT.get(loop.phase, loop.phase)}{since}{step}"
                              + (c.dim(f"  {doing}") if doing else ""), width))
    return lines
