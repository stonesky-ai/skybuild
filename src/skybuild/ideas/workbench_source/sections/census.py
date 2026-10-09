"""The Sub-agents and Who-Where sections: every agent on this box under its scheme name.
"""
from __future__ import annotations

import re
import socket
from typing import Any

from .. import hub as sv


def subagents_section(by_session: list, loops, now: float) -> dict:
    """Each live session with its own sub-agents under it, and each named loop's current step.

    Both halves come from sessionview's own collectors — `sv.read_session_agents`
    (the `agent-<id>` worktrees `git worktree list` shows, owned through the
    session's own `subagents/agent-<id>.meta.json`; its in-process agents; the
    agent CLIs under it in the process tree) and `sv.read_loop_steps` — never
    a copy, so the terminal's block and this box cannot disagree. A session
    with none is an empty list. With no loop state directory named, the loop
    half refuses by the setting's name (its `error`) while the sessions still
    show. Nothing here writes: a worktree is only listed.
    """
    sessions = [{
        "session": entry.session.session_id[:8] if entry.session.session_id else "(unidentified)",
        "pid": entry.session.pid,
        "sub_agents": [{"kind": agent.kind, "name": sv._shown(agent.name), "state": agent.state,
                        "detail": sv._shown(agent.detail), "model": sv._shown(agent.model),
                        "uptime": sv.age(now - agent.started_at) if agent.started_at is not None else "",
                        "doing": sv._shown(agent.doing),
                        "age": sv.age(now - agent.since) if agent.since else ""}
                       for agent in entry.agents],
    } for entry in by_session]
    rows = []
    for loop in loops.loops:
        stepping = loop.phase == "step" and loop.step is not None
        rows.append({
            "loop": sv._shown(loop.stream or "?"),
            "state": sv._CODEX_PHASE_BADGE.get(loop.phase, "unknown"),
            "phase": sv._PHASE_TEXT.get(loop.phase, loop.phase),
            "since": sv.age(now - loop.since) if loop.since else "",
            "step": loop.step.name if stepping else "",
            "doing": sv._shown(loop.step.doing if stepping else loop.note),
            "pid": ",".join(str(pid) for pid in loop.pids),
            "state_dir": str(loop.state_dir),
        })
    return {"sessions": sessions, "loops": {"error": loops.refused, "rows": rows}}


#: The owner-scheme name, the same shape `scripts/agents/agent_heartbeat.py` writes.
#: `<host>.<agent>-sa.<n>` and the hyphen form `hostname-a.SESSION-sa.N` both match.
_SCHEME_NAME = re.compile(
    r"^[a-z][a-z0-9-]*[-.][a-z][a-z0-9-]*\.[A-Za-z0-9-]+-sa\.[1-9][0-9]*$"
)


def _this_box() -> str:
    """This machine's host label, folded the way the skybus folds a kernel hostname."""
    label = socket.gethostname().split(".")[0].strip().lower()
    if not label or any(ch.isspace() for ch in label):
        return "unknown"
    return label


def _scheme_or_raw(agent_id: str) -> tuple[str, str]:
    """(name, flag). A meta id in the owner scheme is the name; anything else is flagged."""
    shown = sv._shown(agent_id)
    if _SCHEME_NAME.fullmatch(agent_id):
        return shown, ""
    return shown, "no scheme name"


def _take_sub(subs: list, agent) -> Any:
    """The sub-agent row `read_session_agents` built for this meta, if it is still here."""
    if agent.worktree:
        for index, sub in enumerate(subs):
            if sub.kind == "worktree" and sub.name == agent.worktree:
                return subs.pop(index)
    for index, sub in enumerate(subs):
        if sub.kind == "in-process" and sub.name == agent.agent_type:
            return subs.pop(index)
    return None


def _who_agent_row(agent, sub, now: float) -> dict:
    name, flag = _scheme_or_raw(agent.agent_id)
    if sub is not None and sub.state != "unreported":
        state = sub.state
        model = sub.model or agent.model
        uptime = sv.age(now - sub.started_at) if sub.started_at is not None else ""
    else:
        state = agent.state
        model = agent.model
        uptime = ""
    return {
        "name": name,
        "flag": flag,
        "kind": "worktree" if agent.worktree else "in-process",
        "state": state,
        "model": sv._shown(model),
        "uptime": uptime,
    }


def who_where_section(by_session: list, agents: list, now: float, host: str | None = None) -> dict:
    """This box, its live sessions, and each session's agents under their scheme names.

    The name is the id of `subagents/agent-<id>.meta.json` when that id is the
    owner scheme; an id that is not is still listed, flagged `no scheme name`,
    and never dropped. A helper process with no meta file is the same. A box
    with no agent is `none running` and is still returned: the block is this
    box now, and peer boxes join it later. Nothing here writes.
    """
    host_name = sv._shown(host) if host is not None else _this_box()
    sessions = []
    any_agent = False
    for entry in by_session:
        sess = entry.session
        subs = list(entry.agents)
        rows = []
        for agent in agents:
            if agent.session_pid != sess.pid:
                continue
            if agent.state == "done" and not agent.worktree:
                continue
            rows.append(_who_agent_row(agent, _take_sub(subs, agent), now))
        for sub in subs:
            if sub.kind != "process":
                continue
            rows.append({
                "name": sv._shown(sub.name),
                "flag": "no scheme name",
                "kind": "process",
                "state": sub.state,
                "model": sv._shown(sub.model),
                "uptime": sv.age(now - sub.started_at) if sub.started_at is not None else "",
            })
        if rows:
            any_agent = True
        label = sess.session_id[:8] if sess.session_id else "(unidentified)"
        sessions.append({"session": label, "agents": rows})
    return {"boxes": [{
        "host": host_name or "unknown",
        "status": "" if any_agent else "none running",
        "sessions": sessions,
    }]}
