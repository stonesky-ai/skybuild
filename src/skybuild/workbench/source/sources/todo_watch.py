"""The todo service's top warning: its health, its queue, and every box's reach record.
"""
from __future__ import annotations

import json
import logging
import os
import re
import stat
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from .. import hub as sv
from ..terminal import ENV

if TYPE_CHECKING:      # names used in annotations only (strings at runtime)
    from ..settings import WebConfig


# ---------------------------------------------------------------- the todo service's top warning
#
# Owner order 2026-10-05: the todo service listens on one box and every box
# must reach it. When this page's box cannot, when a peer box's reach record
# says it cannot, when that record is old or absent, or when open items wait
# with no box holding a claim, the page shows one red warning per problem above
# everything else, each saying in plain words what to change. Everything is
# read beside the page by `TodoWatch` and cached for TODO_PROBE_SECONDS, so a
# hung service never holds up a refresh. No warning carries a token, and none
# carries a URL: the service is named by its host and port only.

#: The URL the service unit's own start-up check calls (scripts/agents/units/skykeep-todo-service.service).
TODO_HEALTH_SETTING = "SKYKEEP_TODO_HEALTH_URL"
#: The directory of reach records, one file per box, that scripts/todo_service/reach_probe.sh writes.
TODO_REACH_DIR_SETTING = ENV + "TODO_REACH_DIR"
#: The other boxes, the Fleet box's own setting (`sessionview_integrator.fleet_boxes`).
TODO_FLEET_SETTING = ENV + "FLEET_BOXES"
#: The service's systemd user unit on its box (scripts/agents/units/), named in the warning.
TODO_SERVICE_UNIT = "skykeep-todo-service.service"
#: Each box's keeper unit, the queue's only taker since the cutover.
TODO_KEEPER_UNIT = "skykeep-session-keeper.service"
#: The session the owner's order names for a tailscale ACL change.
TODO_NETWORK_CONTACT = "skykeep-de"
TODO_INSTALL_DOC = "docs/dev/todo-service-install.md"
TODO_PROBE_SCRIPT = "scripts/todo_service/reach_probe.sh"
#: The install doc's step that sets up the probe on each box.
TODO_PROBE_STEP = "step 9"

#: A reach record's file name is its box; anything else in the directory (a
#: dotfile the probe is still writing, a stray name) is not a record.
_REACH_BOX = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
#: The one line a record holds: `ok <epoch>` or `fail <epoch> <reason>`, the
#: reason one word the probe chose (`timeout`, `http-503`, ...).
_REACH_LINE = re.compile(r"(ok|fail) ([0-9]{1,12})(?: ([A-Za-z0-9._:-]{1,60}))?")
_REACH_READ_BOUND = 256
_REACH_MAX_FILES = 64
#: A record that could not be read, or that is not the one line above.
REACH_UNREADABLE = "unreadable"


def read_reach_records(directory: Path) -> dict[str, tuple[str, float, str]] | None:
    """Box -> (verdict, epoch, reason) from the one-line reach records; None when the directory cannot be listed.

    The verdict is "ok", "fail" or `REACH_UNREADABLE`: a line that is not
    exactly `ok <epoch>` or `fail <epoch> <reason>` is unreadable, never ok.
    Only a regular file is opened and only its first bytes are read, so a FIFO
    or a huge file never stalls a probe; at most `_REACH_MAX_FILES` are read.
    """
    try:
        names = sorted(entry.name for entry in directory.iterdir())
    except OSError:
        return None
    records: dict[str, tuple[str, float, str]] = {}
    for name in names:
        if len(records) >= _REACH_MAX_FILES:
            break
        if not _REACH_BOX.fullmatch(name):
            continue
        path = directory / name
        try:
            if not stat.S_ISREG(path.lstat().st_mode):
                continue
            with path.open("rb") as fh:
                blob = fh.read(_REACH_READ_BOUND)
        except OSError:
            records[name] = (REACH_UNREADABLE, 0.0, "")
            continue
        lines = blob.decode("utf-8", "replace").splitlines()
        found = _REACH_LINE.fullmatch(lines[0].strip()) if lines else None
        if found is None or (found.group(1) == "fail") != (found.group(3) is not None):
            records[name] = (REACH_UNREADABLE, 0.0, "")
            continue
        records[name] = (found.group(1), float(found.group(2)), found.group(3) or "")
    return records


def _unanswered(exc: OSError, timeout: float) -> str:
    """Why a read got no answer, in a word or two: never the exception's text, which may hold the URL."""
    if isinstance(exc, TimeoutError):
        return f"no answer within {timeout:g} s"
    name = exc.args[0] if type(exc) is OSError and exc.args else ""
    # `http_transport` raises a plain OSError carrying only the reason's class name.
    return name if isinstance(name, str) and re.fullmatch(r"[A-Za-z_]{1,60}", name) else type(exc).__name__


def _minutes(seconds: float) -> str:
    return f"{max(0, int(seconds // 60))} min"


def _warning(code: str, subject: str, headline: str, fix: str) -> dict:
    return {"code": code, "subject": subject, "headline": headline, "fix": fix}


@dataclass(frozen=True)
class TodoProbe:
    """One reading of the todo service and the boxes' reach records, taken beside the page."""
    at: float                                   # when the reading was started
    fixed: tuple[dict, ...]                     # warnings that do not age: settings, service, queue read
    host: str                                   # the service's host, from the health URL; "" unknown
    port: str                                   # its port, from the same URL; "" unknown
    queue: tuple[int, int] | None               # (open items, live claims); None: not read
    reach_on: bool                              # a reach directory is named
    records: dict[str, tuple[str, float, str]] | None   # None: the directory cannot be listed
    fleet: tuple[str, ...]


class TodoWatch:
    """The todo service's top warnings: read beside the page, cached, never blocking a refresh.

    `warnings(now)` answers at once from the last reading. When that reading
    is older than `probe_seconds`, it starts one new reading through `spawn`
    (a daemon thread by default) and still answers at once; a reading still
    running is never started twice. Before the first reading finishes there is
    nothing to say, so nothing is shown.

    A box with none of the todo settings is not part of the todo fleet: it
    shows nothing and asks nothing. Once any is set, a missing or refused one
    is itself a warning naming it, never a silent pass.
    """

    def __init__(self, environ: Mapping[str, str], *, probe_seconds: float, timeout_seconds: float,
                 max_age_seconds: float, untaken_minutes: float, transport: Callable | None = None,
                 spawn: Callable[[Callable[[], None]], None] | None = None, node: str | None = None) -> None:
        self.env = environ
        self.probe_seconds = probe_seconds
        self.timeout = timeout_seconds
        self.max_age = max_age_seconds
        self.untaken_seconds = untaken_minutes * 60
        self.transport = transport
        self.spawn = spawn or (lambda fn: threading.Thread(target=fn, name="todo-probe", daemon=True).start())
        self.node = node or os.uname().nodename.split(".")[0]
        self._lock = threading.Lock()
        self._probe: TodoProbe | None = None
        self._running = False
        self._started = 0.0
        self._untaken_since: float | None = None

    @classmethod
    def from_config(cls, wcfg: WebConfig, environ: Mapping[str, str] | None = None) -> TodoWatch:
        return cls(os.environ if environ is None else environ,
                   probe_seconds=wcfg.todo_probe_seconds, timeout_seconds=wcfg.todo_probe_timeout_seconds,
                   max_age_seconds=wcfg.todo_reach_max_age_seconds, untaken_minutes=wcfg.todo_untaken_minutes)

    def _setting(self, name: str) -> str:
        return (self.env.get(name) or "").strip()

    def armed(self) -> bool:
        return any(self._setting(name) for name in
                   (TODO_HEALTH_SETTING, TODO_REACH_DIR_SETTING, sv.TODO_URL_SETTING, sv.TODO_TOKEN_SETTING))

    def warnings(self, now: float) -> list[dict]:
        """The warnings to show now, from the last reading; starts a new reading when that one is due."""
        if not self.armed():
            return []
        with self._lock:
            probe = self._probe
            # A reading that outlived every timeout it could have hit is given up on, not waited for.
            stuck = self._running and now - self._started > self.probe_seconds + 4 * self.timeout
            due = (not self._running or stuck) and (
                probe is None or not 0 <= now - probe.at < self.probe_seconds)
            if due:
                self._running, self._started = True, now
        if due:
            try:
                self.spawn(lambda: self._run(now))
            except RuntimeError:            # no thread could start: the last reading stays shown
                with self._lock:
                    self._running = False
        with self._lock:
            probe, since = self._probe, self._untaken_since
        return [] if probe is None else self._compose(probe, now, since)

    def _run(self, started: float) -> None:
        try:
            probe = self._read(started)
        except Exception as exc:  # said on the page, never a failed refresh
            logging.getLogger(__name__).exception("todo probe failed")
            probe = TodoProbe(at=started, fixed=(_warning(
                "probe", "", f"TODO SERVICE NOT CHECKED FROM {self.node}",
                f"The check itself failed ({type(exc).__name__}): read sessionview_web's own log on {self.node}."),),
                host="", port="", queue=None, reach_on=False, records={}, fleet=())
        with self._lock:
            # "Untaken" is counted only over readings that saw the queue: one
            # that could not (the service down, a refused token) restarts it, so
            # a recovery is not met with an alarm about the outage's minutes.
            open_items, live = probe.queue if probe.queue is not None else (0, 0)
            if open_items and not live:
                if self._untaken_since is None:
                    self._untaken_since = started
            else:
                self._untaken_since = None
            self._probe, self._running = probe, False

    def _read(self, started: float) -> TodoProbe:
        todo_client = sv._load("sessionview_web_todo_client", "todo_service/client.py")
        transport = self.transport or todo_client.http_transport
        fixed: list[dict] = []
        host = port = ""
        health_ok = False
        raw = self._setting(TODO_HEALTH_SETTING)
        if not raw:
            fixed.append(_warning(
                "config", TODO_HEALTH_SETTING, f"TODO SERVICE NOT CHECKED FROM {self.node}",
                f"Set {TODO_HEALTH_SETTING} in sessionview_web's environment on {self.node} to the todo "
                "service's /health URL, the same value as in ~/.config/skykeep/todo-service.conf on the "
                "service's box."))
        else:
            try:
                url = todo_client.check_url(raw)
            except todo_client.ClientConfigError as exc:
                fixed.append(_warning(
                    "config", TODO_HEALTH_SETTING, f"TODO SERVICE NOT CHECKED FROM {self.node}",
                    f"{TODO_HEALTH_SETTING} is refused ({exc}): correct it in sessionview_web's environment "
                    f"on {self.node}."))
            else:
                parts = urlparse(url)
                host = parts.hostname or ""
                port = str(parts.port or (443 if parts.scheme == "https" else 80))
                why = self._ask_health(transport, url)
                health_ok = not why
                if why:
                    fixed.append(_warning(
                        "service_down", "", f"TODO SERVICE UNREACHABLE FROM {self.node}: {why}",
                        f"On {host} run `systemctl --user status {TODO_SERVICE_UNIT}` and start it if it is "
                        f"not active. If it is active there, the network from {self.node} to {host} port "
                        f"{port} is blocked (a tailscale ACL)."))
        queue = None
        if health_ok:
            queue, why = self._read_queue(todo_client, transport)
            if why:
                fixed.append(_warning("queue_unreadable", "", f"TODO QUEUE UNREADABLE FROM {self.node}", why))
        reach = self._setting(TODO_REACH_DIR_SETTING)
        records = read_reach_records(Path(reach).expanduser()) if reach else {}
        fleet = tuple(box for box in self._setting(TODO_FLEET_SETTING).replace(",", " ").split()
                      if _REACH_BOX.fullmatch(box))
        return TodoProbe(at=started, fixed=tuple(fixed), host=host, port=port, queue=queue,
                         reach_on=bool(reach), records=records, fleet=fleet)

    def _ask_health(self, transport: Callable, url: str) -> str:
        """"" when /health answers 200, else why not. No token: /health needs none."""
        try:
            status, _body = transport("GET", url, {}, None, self.timeout)
        except OSError as exc:
            return _unanswered(exc, self.timeout)
        return "" if status == 200 else f"it answered HTTP {status}"

    def _read_queue(self, todo_client, transport: Callable) -> tuple[tuple[int, int] | None, str]:
        """((open items, live claims), "") from /queue with this box's token, or (None, what to change)."""
        raw_url, token_file = self._setting(sv.TODO_URL_SETTING), self._setting(sv.TODO_TOKEN_SETTING)
        if not raw_url:
            return None, (f"Set {sv.TODO_URL_SETTING} in sessionview_web's environment on {self.node} to the "
                          "todo service's base URL, so this page can tell whether any box takes the queue.")
        if not token_file:
            return None, (f"Set {sv.TODO_TOKEN_SETTING} in sessionview_web's environment on {self.node} to "
                          "this box's todo token file (mode 0600).")
        try:
            base = todo_client.check_url(raw_url)
            token = todo_client.read_token(token_file)
        except todo_client.ClientConfigError as exc:
            return None, (f"{sv.TODO_URL_SETTING} or {sv.TODO_TOKEN_SETTING} is refused ({exc}): correct it in "
                          f"sessionview_web's environment on {self.node}.")
        try:
            status, body = transport("GET", base + "/queue", {"Authorization": f"Bearer {token}"}, None,
                                     self.timeout)
        except OSError as exc:
            return None, f"/queue gave no answer ({_unanswered(exc, self.timeout)}) though /health did: try again."
        if status in (401, 403):
            return None, (f"The service refuses this box's token (HTTP {status}): hand {self.node} its token "
                          f"again ({TODO_INSTALL_DOC} step 5) and point {sv.TODO_TOKEN_SETTING} at that file.")
        if status != 200:
            return None, (f"The service answered HTTP {status} to /queue: on its box read "
                          f"`journalctl --user -u {TODO_SERVICE_UNIT} -n 30`.")
        try:
            doc = json.loads(body)
            open_items, claimed = doc["open"], doc["claimed"]
            if not isinstance(open_items, list) or not isinstance(claimed, list):
                raise TypeError
        except (ValueError, TypeError, KeyError):
            return None, (f"The service's /queue answer does not parse: the service and this page are from "
                          f"different versions; deploy one sha to both ({TODO_INSTALL_DOC}).")

        def live(claim: object) -> bool:
            # A claim whose heartbeat is as old as the window holds nothing.
            age = claim.get("heartbeat_age_seconds") if isinstance(claim, dict) else None
            return not (isinstance(age, (int, float)) and age >= self.untaken_seconds)

        return (len(open_items), sum(1 for claim in claimed if live(claim))), ""

    def _compose(self, probe: TodoProbe, now: float, untaken_since: float | None) -> list[dict]:
        out = list(probe.fixed)
        if (probe.queue is not None and probe.queue[0] and untaken_since is not None
                and now - untaken_since >= self.untaken_seconds):
            out.append(_warning(
                "queue_untaken", "",
                f"TODO QUEUE NOT TAKEN: {probe.queue[0]} open, no box has held a claim for "
                f"{_minutes(now - untaken_since)}",
                f"Check each box's keeper: `systemctl --user status {TODO_KEEPER_UNIT}` and "
                f"`journalctl --user -u {TODO_KEEPER_UNIT} -n 30`. A keeper that cannot reach the service, "
                "or whose token it refuses, starts nothing."))
        if not probe.reach_on:
            return out                      # no directory named: no peer rows, and no guess at any
        if probe.records is None:
            out.append(_warning(
                "reach_unreadable", "", "REACH RECORDS UNREADABLE",
                f"{TODO_REACH_DIR_SETTING} names a directory this page cannot list: create it, or correct "
                f"the setting in sessionview_web's environment on {self.node}."))
            return out
        host, port = probe.host or "the todo service's box", probe.port or "<the service port>"
        install = f"({TODO_INSTALL_DOC} {TODO_PROBE_STEP})"
        for box in sorted((set(probe.records) | set(probe.fleet)) - {self.node}):
            found = probe.records.get(box)
            if found is None:
                out.append(_warning(
                    "box_missing", box, f"{box}: NO REACH RECORD",
                    f"Install {TODO_PROBE_SCRIPT} on {box} with a timer that writes its record into "
                    f"{TODO_REACH_DIR_SETTING} on {self.node} {install}."))
                continue
            verdict, epoch, reason = found
            age = now - epoch
            if verdict == REACH_UNREADABLE:
                out.append(_warning(
                    "box_unreadable", box, f"{box}: REACH RECORD UNREADABLE",
                    f"The record must be one line, `ok <epoch>` or `fail <epoch> <reason>`: reinstall "
                    f"{TODO_PROBE_SCRIPT} on {box} {install}."))
            elif age < -self.max_age:
                out.append(_warning(
                    "box_unreadable", box, f"{box}: REACH RECORD FROM THE FUTURE",
                    f"Its time is {_minutes(-age)} ahead of {self.node}: check the clock on {box} "
                    "(`timedatectl`)."))
            elif age > self.max_age:
                last = verdict + (f" ({reason})" if reason else "")
                out.append(_warning(
                    "box_stale", box, f"{box}: REACH RECORD STALE ({_minutes(age)} old, limit "
                    f"{_minutes(self.max_age)})",
                    f"{box} last said {last}, too long ago to count as ok. On {box} check that "
                    f"{TODO_PROBE_SCRIPT} still runs on its timer and can write to {self.node} {install}."))
            elif verdict == "fail" and reason.startswith("http-"):
                out.append(_warning(
                    "box_fail", box, f"{box} GETS AN ERROR FROM THE TODO SERVICE ({reason}, "
                    f"{_minutes(age)} ago)",
                    f"The network works but the service answers an error: on {host} run "
                    f"`systemctl --user status {TODO_SERVICE_UNIT}` and "
                    f"`journalctl --user -u {TODO_SERVICE_UNIT} -n 30`."))
            elif verdict == "fail":
                out.append(_warning(
                    "box_fail", box, f"{box} CANNOT REACH THE TODO SERVICE ({reason}, {_minutes(age)} ago)",
                    f"tailscale may block port {port} to {host}: add an ACL or grant allowing tcp:{port} "
                    f"from {box} to {host} in the tailscale admin console, or tell {TODO_NETWORK_CONTACT}."))
        return out
