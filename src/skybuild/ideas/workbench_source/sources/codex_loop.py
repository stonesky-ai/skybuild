"""The Codex loop: its log, its iterations, the model of each, its limits and NEEDS-OWNER state.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .. import hub as sv

if TYPE_CHECKING:      # names used in annotations only (strings at runtime)
    from ..settings import Config


# ---------------------------------------------------------------- codex loop

# An unattended Codex stream runs as a loop outside every Claude session: each
# step is a fresh `codex exec` that reads the stream's state file, works, saves
# and exits. None of it is a session or a sub-agent, so without this block a
# checkout that tests and pushes all night reads as nothing happening.

_LOG_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (.*)$")
_LOOP_START = re.compile(r"^loop start: (\S+) in (.+?), profile ")
_ITER_START = re.compile(r"^iteration start \((iter-(\d{8}T\d{6})\.jsonl)\)")
_ITER_END = re.compile(r"^iteration end: (\S+)\s*(.*)$")
_ROLLOUT = re.compile(r"^rollout-(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})-")
_MODEL = re.compile(r'"model"\s*:\s*"([^"]+)"')
_BASH_WRAP = re.compile(r"^\S*bash -lc (['\"])(.*)\1$", re.DOTALL)


@dataclass
class CodexStep:
    name: str                  # iter-<stamp>.jsonl in the state directory
    started: float
    doing: str = ""            # "running: <cmd>" or "said: <text>"
    commands: int = 0
    tokens: str = ""           # set once the step's turn completes
    model: str | None = None


@dataclass
class CodexLoop:
    state_dir: Path
    pids: list[int] = field(default_factory=list)
    stream: str | None = None
    checkout: Path | None = None
    started: float | None = None           # the last `loop start`
    phase: str = "never"   # step | napping | limit-wait | memory-wait | stopped | never
    since: float | None = None
    note: str = ""
    recent: list[tuple[float, str, str]] = field(default_factory=list)   # newest first
    step: CodexStep | None = None
    state: dict[str, str] = field(default_factory=dict)
    state_age: float | None = None
    limits: list[tuple[str, float]] = field(default_factory=list)
    stop_file: bool = False
    needs_owner: str | None = None

    @property
    def running(self) -> bool:
        return bool(self.pids)


def apply_loop_log(loop: CodexLoop, text: str) -> None:
    """Replay the loop's log into `loop`: phase, last start, recent outcomes, last step.

    The log is the loop's only account of itself, so its last line decides the
    phase — except that a loop whose process is gone is stopped whatever its
    log last said: a loop killed mid-step never writes an end line.
    """
    for line in text.splitlines():
        m = _LOG_LINE.match(line)
        if not m:
            continue
        try:
            ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").astimezone().timestamp()
        except ValueError:
            continue
        msg = m.group(2)
        if hit := _LOOP_START.match(msg):
            loop.stream, loop.checkout = hit.group(1), Path(hit.group(2))
            loop.started = ts
            loop.phase, loop.since, loop.note = "napping", ts, "loop started"
        elif hit := _ITER_START.match(msg):
            loop.step = CodexStep(hit.group(1), ts)
            loop.phase, loop.since, loop.note = "step", ts, ""
        elif hit := _ITER_END.match(msg):
            loop.recent.insert(0, (ts, hit.group(1), hit.group(2)))
            loop.phase, loop.since, loop.note = "napping", ts, f"after {hit.group(1)}"
        elif msg.startswith("usage limit: sleeping"):
            loop.phase, loop.since, loop.note = "limit-wait", ts, msg.removeprefix("usage limit: ")
        elif msg.startswith("MemAvailable"):
            loop.phase, loop.since, loop.note = "memory-wait", ts, msg
        elif msg.startswith("STOP file present"):
            loop.phase, loop.since, loop.note = "stopped", ts, "STOP file"
        elif msg.startswith(("NOTIFY ", "Codex asked the loop to stop")):
            loop.phase, loop.since, loop.note = "stopped", ts, msg.removeprefix("NOTIFY ")
    del loop.recent[3:]
    if not loop.running and loop.phase != "never":
        if loop.phase == "step":
            loop.note = "exited mid-step (killed, or its unit was stopped)"
        elif loop.phase != "stopped" and loop.recent:
            loop.note = f"exited after {loop.recent[0][1]} {loop.recent[0][2]}".strip()
        loop.phase = "stopped"


def summarize_step(path: Path, window: int = 524288) -> tuple[str, int, str]:
    """(what the step is doing or last said, commands seen, token usage).

    `codex exec --json` writes one event per line and carries each command's
    whole output, so a long step runs to megabytes: only the tail is read. A
    command started and not yet completed is what the step is doing now.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            fh.seek(max(0, size - window))
            chunk = fh.read()
    except OSError:
        return "", 0, ""
    lines = chunk.split(b"\n")
    if size > window:
        lines = lines[1:]                              # the first line is partial
    open_cmds: dict[str, str] = {}
    said, tokens, commands = "", "", 0
    for raw in lines:
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        kind = event.get("type")
        item = event.get("item") or {}
        if item.get("type") == "command_execution":
            if kind == "item.started":
                open_cmds[str(item.get("id"))] = str(item.get("command") or "")
                commands += 1
            elif kind == "item.completed":
                open_cmds.pop(str(item.get("id")), None)
        elif item.get("type") == "agent_message" and kind == "item.completed":
            said = str(item.get("text") or "")
        elif kind == "turn.completed":
            usage = event.get("usage") or {}
            tokens = (f"{usage.get('input_tokens', 0):,} in "
                      f"({usage.get('cached_input_tokens', 0):,} cached), "
                      f"{usage.get('output_tokens', 0):,} out")
    if open_cmds:
        doing = "running: " + _short_command(list(open_cmds.values())[-1])
    elif said:
        doing = "said: " + " ".join(said.split())
    else:
        doing = ""
    return doing, commands, tokens


def _short_command(cmd: str) -> str:
    m = _BASH_WRAP.match(cmd.strip())
    return " ".join((m.group(2) if m else cmd).split())


def step_model(codex_home: Path, step: CodexStep, checkout: Path | None) -> str | None:
    """The model of the Codex session a loop step started.

    Codex names each session's rollout by its local start time, a second or
    two after the loop logs the step. The candidate must also be an `exec`
    session in the loop's checkout: the owner's interactive window writes
    rollouts into the same directory.
    """
    best: tuple[float, Path] | None = None
    days = {sv.local(step.started).date(), sv.local(step.started + 60).date()}
    for day in days:
        folder = codex_home / "sessions" / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"
        for path in folder.glob("rollout-*.jsonl"):
            m = _ROLLOUT.match(path.name)
            if not m:
                continue
            try:
                ts = datetime.strptime(m.group(1), "%Y-%m-%dT%H-%M-%S").astimezone().timestamp()
            except ValueError:
                continue
            if not 0 <= ts - step.started <= 60:
                continue
            meta = _rollout_meta(path)
            if meta.get("originator") != "codex_exec":
                continue
            if checkout is not None and meta.get("cwd") != str(checkout):
                continue
            if best is None or ts < best[0]:
                best = (ts, path)
    if best is None:
        return None
    # Codex records the model in each turn's context, and an exec step is one
    # turn: the record sits near the head, behind a megabyte of command output.
    # A later turn's record, when there is one, is in the tail and wins.
    window = 262144
    try:
        size = best[1].stat().st_size
        with best[1].open("rb") as fh:
            head = fh.read(window).decode("utf-8", "replace")
            fh.seek(max(window, size - window))
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return None
    models = _MODEL.findall(tail) or _MODEL.findall(head)
    return models[-1] if models else None


def _rollout_meta(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            first = json.loads(fh.readline())
    except (OSError, ValueError):
        return {}
    payload = first.get("payload") if isinstance(first, dict) else None
    return payload if isinstance(payload, dict) else {}


def read_limits(path: Path) -> list[tuple[str, float]]:
    """(window label, used percent) for each usage window Codex last reported."""
    try:
        snap = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out: list[tuple[str, float]] = []
    for key in ("primary", "secondary"):
        window = snap.get(key) if isinstance(snap, dict) else None
        if not isinstance(window, dict):
            continue
        minutes = int(window.get("window_minutes") or 0)
        if minutes >= 7 * 24 * 60:
            label = "weekly"
        elif minutes >= 60:
            label = f"{minutes // 60}h"
        else:
            label = f"{minutes}m"
        try:
            out.append((label, float(window.get("used_percent") or 0)))
        except (TypeError, ValueError):
            continue
    return out


def read_state_file(path: Path) -> dict[str, str]:
    """The stream's `key: value` state file (docs/dev/streams/<stream>.md § Unattended)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    state: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"^([a-z_]+):\s*(.*)$", line)
        if m:
            state[m.group(1)] = m.group(2).strip()
    return state


class CodexReader:
    """Reads the loop each refresh; a finished step's model never changes, so it is cached."""

    def __init__(self) -> None:
        self._models: dict[str, str | None] = {}

    def read(self, cfg: Config, now: float) -> CodexLoop | None:
        if cfg.codex_state is None:
            return None
        state_dir, checkout, pids = cfg.codex_state, None, []
        for pid, cmd in sv.iter_processes():
            argv = cmd.split()
            if not any(Path(arg).name == cfg.codex_loop_pattern for arg in argv[:2]):
                continue
            pids.append(pid)
            state_dir = Path(_flag(argv, "--state-dir") or state_dir).expanduser()
            checkout = _flag(argv, "--checkout")
        if not pids and not state_dir.is_dir():
            return None                                # no loop on this machine

        loop = CodexLoop(state_dir, pids)
        try:
            apply_loop_log(loop, (state_dir / "loop.log").read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass
        if checkout:
            loop.checkout = Path(checkout).expanduser()
        if loop.step:
            loop.step.doing, loop.step.commands, loop.step.tokens = summarize_step(
                state_dir / loop.step.name)
            if loop.step.name not in self._models or loop.phase == "step":
                self._models[loop.step.name] = step_model(cfg.codex_home or Path.home() / ".codex",
                                                          loop.step, loop.checkout)
            loop.step.model = self._models[loop.step.name]
        if loop.checkout and loop.stream:
            state_path = loop.checkout / ".agent-state" / f"{loop.stream}.md"
            loop.state = read_state_file(state_path)
            try:
                loop.state_age = now - state_path.stat().st_mtime
            except OSError:
                loop.state_age = None
        loop.limits = read_limits(state_dir / "limits.json")
        loop.stop_file = (state_dir / "STOP").exists()
        loop.needs_owner = _needs_owner(state_dir / "NEEDS-OWNER.md", loop)
        return loop


def _flag(argv: list[str], name: str) -> str | None:
    for i, arg in enumerate(argv):
        if arg == name and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1]
    return None


def _needs_owner(path: Path, loop: CodexLoop) -> str | None:
    """The loop's last call for the owner — unless a later start has answered it.

    The file is never deleted, and restarting the loop is how the owner says
    the question is settled, so a note older than the last start is history.
    """
    try:
        written = path.stat().st_mtime
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if loop.started is not None and written < loop.started:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    title = lines[0].lstrip("# ") if lines else "NEEDS-OWNER.md"
    body = lines[-1] if len(lines) > 1 else ""
    return f"{title}: {body}" if body and body != title else title
