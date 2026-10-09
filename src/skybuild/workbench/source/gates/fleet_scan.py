"""A box's own fleet scan: sessions, headless runs, keepers and loops from `ps`, command lines with
every credential hidden.
"""
from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import hub as sv

# ---------------------------------------------------------------- a remote box's lanes, over ssh

#: A box is probed over ssh at most this often, and never from the page's own thread.
REMOTE_PROBE_TTL_SECONDS = 30.0
#: After a probe fails or hangs the box is left alone this long: a hang can be an auth prompt (Tailscale SSH
#: `check`) waiting on a person, and retrying would raise another one every few seconds.
REMOTE_PROBE_BACKOFF_SECONDS = 600.0
REMOTE_PROBE_TIMEOUT_SECONDS = 25.0


_CODEX_HELPERS = ("app-server", "sandbox", "code-mode-host", "pid-update-loop", "linux-sandbox")
_KEEPERS = (("session_keeper.py", "session-keeper"), ("skykeep-codex-loop", "codex-loop"),
            ("skykeep-grok-loop", "grok-loop"), ("autocodex", "autocodex"), ("autogrok", "autogrok"),
            ("skykeep-watchdog", "watchdog"), ("gate_run.py", "gate_run"), ("seam_merge.py", "seam_merge"),
            ("seam_land.py", "seam_land"), ("claim.py", "claim"))


def _elapsed(seconds: int) -> str:
    if seconds > 365 * 86400:
        return "unknown"         # a start stamp of 0 measured from now is no age
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    return (f"{days}d" if days else "") + (f"{hours}h" if hours or days else "") + f"{minutes:02d}m" if days or hours \
        else f"{minutes}m{int(seconds) % 60:02d}s"


# Redaction runs in this order, and before a command line is cut to length. The fleet table
# and the saved snapshot both show these lines, so a credential that slips through is published.
# URL userinfo (a username/password pair or token): everything before the last @ ahead of the path.
_SECRET_URL = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/?#]*@")
# An Authorization header: the scheme AND the credential. A scheme-shaped first word takes the next word with it.
_SECRET_AUTH_HEADER = re.compile(r"(?i)(authorization\s*[:=]\s*)(?:[a-z][a-z0-9-]*\s+)?\S+")
# curl's -u/-U/--user/--proxy-user user:password (also -uuser:password); a value with no colon carries no password.
_SECRET_USER = re.compile(r"(?<!\S)(-[uU]\s*|--(?:proxy-)?user(?:\s+|=))[^\s:]*:\S*")
_SECRET = re.compile(r"(?i)((?:token|key|secret|password|passwd|auth|bearer)[\w-]*[=\s:]+)\S+")
_NOISE = ("bash -c source", "/bin/bash -c", "ps -eo", "grep ", "codegraph", "snapshot-bash")


def _cwd_of(pid: int) -> str:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return ""


def _branch_of(cwd: str) -> str:
    if not cwd or cwd == str(Path.home()):
        return ""
    return sv.run_text(["git", "-C", cwd, "branch", "--show-current"]).strip()


def _cmdline(args: str, limit: int = 170) -> str:
    text = _SECRET_URL.sub(r"\1***@", args)
    text = _SECRET_AUTH_HEADER.sub(r"\1***", text)
    text = _SECRET_USER.sub(r"\1***", text)
    return _SECRET.sub(r"\1***", text)[:limit]


# An error line's credential: a named value (`token=`, `password:`, `api_key=`) or a bearer one. Narrower than
# `_SECRET` on purpose: "Host key verification failed" must still say what failed.
_SECRET_PAIR = re.compile(r"(?i)\b((?:[\w-]*(?:token|secret|password|passwd|api[_-]?key))\s*[=:]\s*|bearer\s+)\S+")


def _redact(text: str, limit: int = 240) -> str:
    """An ssh failure as the page may show it: URL userinfo, auth headers, user:password and named secrets hidden."""
    text = _SECRET_URL.sub(r"\1***@", text)
    text = _SECRET_AUTH_HEADER.sub(r"\1***", text)
    text = _SECRET_USER.sub(r"\1***", text)
    return _SECRET_PAIR.sub(r"\1***", text)[:limit]


def _descendants(procs: list[dict], root: int) -> list[dict]:
    kids: dict[int, list[dict]] = {}
    for proc in procs:
        kids.setdefault(proc["ppid"], []).append(proc)
    out, todo = [], [root]
    while todo:
        for kid in kids.get(todo.pop(), []):
            out.append(kid)
            todo.append(kid["pid"])
    return out


def fleet_scan(procs: list[dict], cwd_of: Callable[[int], str] = _cwd_of,
               branch_of: Callable[[str], str] = _branch_of) -> dict:
    """What agents and keepers one box runs, from its `ps` rows: counts, longest uptimes, keeper names.

    Claude `-p` runs are headless (a keeper or loop started them); the others are editor or terminal sessions.
    A sub-agent that lives inside its session leaves no process, so `ps` cannot count those.
    """
    out: dict[str, Any] = {"claude": [], "claude_headless": [], "codex": [], "grok": [], "keepers": []}
    for proc in procs:
        argv = sv._argv(proc["args"])
        if not argv:
            continue
        exe = Path(argv[0]).name
        # a python/bash wrapper names its script in argv[1:3]; a program is its own argv[0]
        named = {exe} | ({Path(a).name for a in argv[1:3]} if exe in ("python3", "python", "bash", "sh", "node") else set())
        keeper = next((label for key, label in _KEEPERS if key in named), None)
        if keeper:
            stream = argv[argv.index("--stream") + 1] if "--stream" in argv and argv.index("--stream") + 1 < len(argv) else ""
            out["keepers"].append({"name": keeper, "stream": stream, "elapsed": proc["elapsed"], "pid": proc["pid"],
                                   "cmd": _cmdline(proc["args"])})
            continue
        row = {"pid": proc["pid"], "elapsed": proc["elapsed"]}
        if exe == "claude":
            kind = "claude_headless" if "-p" in argv[1:] or "--print" in argv else "claude"
        elif exe == "codex" and not any(h in proc["args"] for h in _CODEX_HELPERS):
            kind = "codex"
        elif exe == "grok":
            kind = "grok"
        else:
            continue
        out[kind].append(row)
    # every root's cwd, branch, and process tree: what it works on and what it spawned
    procs_by_pid = {proc["pid"]: proc for proc in procs}
    for kind in ("claude", "claude_headless", "codex", "grok", "keepers"):
        for row in out[kind]:
            cwd = cwd_of(row["pid"])
            row["cwd"] = cwd
            row["branch"] = branch_of(cwd)
            row["cmd"] = row.get("cmd") or _cmdline(procs_by_pid[row["pid"]]["args"])
            tree = _descendants(procs, row["pid"])
            row["children"] = len(tree)
            row["tree"] = [{"pid": t["pid"], "parent": t["ppid"], "elapsed": t["elapsed"], "cmd": _cmdline(t["args"])}
                           for t in tree if not any(n in t["args"] for n in _NOISE)][:12]
    return out


def _roots(scan: dict) -> list[tuple[str, dict]]:
    return [(kind, row) for kind in ("claude", "claude_headless", "codex", "grok", "keepers") for row in scan.get(kind, [])]
