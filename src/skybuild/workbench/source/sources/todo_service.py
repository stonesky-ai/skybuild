"""The todo service's documents the page draws: the alarms, the queue and the integration board.

Each is read beside the page with this box's token (in the Authorization header only, never
in a URL or on the page), at most every TODO_PROBE_SECONDS and with the probe timeout, so a
slow or dead service never holds up a refresh. A read that fails answers "no answer from the
todo service" with the reason in plain words, and the page draws that instead of the data.
An answer is data: the page puts every string of it on the page as text, never as markup.
"""
from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

from .. import hub as sv
from .seam_status import build_seam_status
from .todo_watch import TODO_INSTALL_DOC, TODO_SERVICE_UNIT, _unanswered

if TYPE_CHECKING:      # names used in annotations only (strings at runtime)
    from ..settings import WebConfig

#: The service documents the page draws, by section key: the path read and what the doc must hold.
TODO_DOCUMENTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "alarms": ("/alarms", ("alarms",)),
    "queue": ("/queue", ("open", "claimed", "blocked")),
    "queue_lines": ("/queue/lines", ("models", "blocked", "migrations")),
    "seamstatus": ("/seams", ("seams",)),
    "integration": ("/integration", ("sets",)),
    "landing": ("/metrics/landing", ("rows", "days", "landed_today")),
}
NO_ANSWER = "no answer from the todo service"
#: A section's standing when the page is on a box with none of the todo settings.
NOT_CONFIGURED = "not configured"


class TodoReader:
    """The service's documents, cached per path, read beside the page; never blocking a refresh.

    `section(key, now)` answers at once from the last reading of that document and starts one
    new reading through `spawn` (a daemon thread by default) when the last is older than
    `probe_seconds`; a reading still running is never started twice. Before the first reading
    finishes the section says so. A box with none of the todo settings is not part of the todo
    fleet: every section says "not configured" and nothing is asked.
    """

    def __init__(self, environ: Mapping[str, str], *, probe_seconds: float, timeout_seconds: float,
                 transport: Callable | None = None,
                 spawn: Callable[[Callable[[], None]], None] | None = None, node: str | None = None) -> None:
        self.env = environ
        self.probe_seconds = probe_seconds
        self.timeout = timeout_seconds
        self.transport = transport
        self.spawn = spawn or (lambda fn: threading.Thread(target=fn, name="todo-read", daemon=True).start())
        self.node = node or os.uname().nodename.split(".")[0]
        self._lock = threading.Lock()
        self._last: dict[str, dict] = {}
        self._running: dict[str, float] = {}

    @classmethod
    def from_config(cls, wcfg: WebConfig, environ: Mapping[str, str] | None = None) -> TodoReader:
        return cls(os.environ if environ is None else environ, probe_seconds=wcfg.todo_probe_seconds,
                   timeout_seconds=wcfg.todo_probe_timeout_seconds)

    def _setting(self, name: str) -> str:
        return (self.env.get(name) or "").strip()

    def configured(self) -> str:
        """"" when the service URL and this box's token file are both named, else what to set."""
        for name in (sv.TODO_URL_SETTING, sv.TODO_TOKEN_SETTING):
            if not self._setting(name):
                return (f"{NOT_CONFIGURED}: set {name} in sessionview_web's environment on {self.node} "
                        f"({TODO_INSTALL_DOC} step 9)")
        return ""

    def _client(self) -> tuple[str, str, object] | tuple[None, None, str]:
        """(base URL, token, client module), or (None, None, why) when the settings are refused."""
        todo_client = sv._load("sessionview_web_todo_client", "todo_service/client.py")
        try:
            base = todo_client.check_url(self._setting(sv.TODO_URL_SETTING))
            token = todo_client.read_token(self._setting(sv.TODO_TOKEN_SETTING))
        except todo_client.ClientConfigError as exc:
            return None, None, (f"{sv.TODO_URL_SETTING} or {sv.TODO_TOKEN_SETTING} is refused ({exc}): "
                                f"correct it in sessionview_web's environment on {self.node}")
        return base, token, todo_client

    def _fetch(self, path: str, fields: tuple[str, ...]) -> tuple[dict | None, str]:
        """(the document, "") or (None, why not) for one path, with this box's token."""
        base, token, todo_client = self._client()
        if base is None:
            return None, str(todo_client)
        transport = self.transport or todo_client.http_transport
        try:
            status, body = transport("GET", base + path, {"Authorization": f"Bearer {token}"}, None, self.timeout)
        except OSError as exc:
            return None, (f"{NO_ANSWER} ({_unanswered(exc, self.timeout)}): on its box run "
                          f"`systemctl --user status {TODO_SERVICE_UNIT}`")
        if status in (401, 403):
            return None, (f"the todo service refuses this box's token (HTTP {status}): hand {self.node} its "
                          f"token again ({TODO_INSTALL_DOC} step 5)")
        if status == 404:
            return None, (f"the todo service has no {path} (HTTP 404): it runs older code than this page; "
                          f"deploy one sha to both ({TODO_INSTALL_DOC})")
        if status != 200:
            return None, (f"the todo service answered HTTP {status} to {path}: on its box read "
                          f"`journalctl --user -u {TODO_SERVICE_UNIT} -n 30`")
        try:
            doc = json.loads(body)
        except ValueError:
            doc = None
        if not isinstance(doc, dict) or any(field not in doc for field in fields):
            return None, (f"the todo service's {path} answer does not parse: the service and this page are "
                          f"from different versions; deploy one sha to both ({TODO_INSTALL_DOC})")
        return doc, ""

    def _read(self, key: str, started: float) -> None:
        path, fields = TODO_DOCUMENTS[key]
        try:
            doc, why = self._fetch(path, fields)
        except Exception as exc:  # noqa: BLE001 - said in the section, never a failed refresh
            doc, why = None, f"{NO_ANSWER}: the read itself failed ({type(exc).__name__})"

        # For seamstatus, fetch items and join them
        if key == "seamstatus" and doc is not None:
            by_id = {}
            items_failed = 0
            for state in ("pushed", "claimed", "open"):
                items_doc, _ = self._fetch(f"/items?state={state}", ("items",))
                if items_doc is not None and isinstance(items_doc.get("items"), list):
                    for item in items_doc["items"]:
                        if isinstance(item, dict) and isinstance(item.get("id"), str):
                            by_id[item["id"]] = item
                else:
                    items_failed += 1
            doc = build_seam_status(doc, by_id)
            if items_failed == 3:
                doc["note"] = "priorities and finish dates unavailable: the todo service did not answer /items"

        with self._lock:
            self._last[key] = {"state": "ok", "doc": doc, "at": started} if doc is not None else \
                {"state": "no_answer", "why": why, "at": started}
            self._running.pop(key, None)

    def section(self, key: str, now: float) -> dict:
        """The section for `key` now: `{"state": "ok", "read_at": ..., **doc}` or `{"state": "no_answer", "why": ...}`."""
        why = self.configured()
        if why:
            return {"state": "no_answer", "why": why}
        with self._lock:
            last = self._last.get(key)
            started = self._running.get(key)
            # A reading that outlived every timeout it could have hit is given up on, not waited for.
            stuck = started is not None and now - started > self.probe_seconds + 4 * self.timeout
            due = (started is None or stuck) and (last is None or not 0 <= now - last["at"] < self.probe_seconds)
            if due:
                self._running[key] = now
        if due:
            try:
                self.spawn(lambda: self._read(key, now))
            except RuntimeError:                # no thread could start: the last reading stays shown
                with self._lock:
                    self._running.pop(key, None)
        with self._lock:
            last = self._last.get(key)
        if last is None:
            return {"state": "no_answer", "why": "not read yet: the first reading is on its way"}
        if last["state"] != "ok":
            return {"state": "no_answer", "why": last["why"], "read_at": last["at"]}
        section = {"state": "ok", "read_at": last["at"], "age": now - last["at"], **last["doc"]}
        if key == "queue":
            section["lines"] = self.section("queue_lines", now)
        return section

    def forget(self, key: str) -> None:
        """Drop the cached reading, so the next `section` call reads again (after a write)."""
        with self._lock:
            self._last.pop(key, None)

    def ack_alarm(self, alarm_id: object, session: str) -> tuple[int, dict]:
        """POST /alarms/<id>/ack with this box's token: (status, what to tell the page)."""
        if type(alarm_id) is not int or isinstance(alarm_id, bool) or alarm_id < 1:
            return 400, {"refused": "an alarm id is a positive integer"}
        why = self.configured()
        if why:
            return 503, {"refused": why}
        base, token, todo_client = self._client()
        if base is None:
            return 503, {"refused": str(todo_client)}
        transport = self.transport or todo_client.http_transport
        body = json.dumps({"session": session}).encode("utf-8")
        try:
            status, answer = transport("POST", f"{base}/alarms/{alarm_id}/ack",
                                       {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                                       body, self.timeout)
        except OSError as exc:
            return 502, {"refused": f"{NO_ANSWER} ({_unanswered(exc, self.timeout)})"}
        self.forget("alarms")
        try:
            payload = json.loads(answer)
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        if status == 200:
            return 200, {"acked": alarm_id}
        error = payload.get("error")
        return (status if 400 <= status < 600 else 502), {
            "refused": f"the todo service answered HTTP {status}" + (f" ({error})" if isinstance(error, str) else "")}
