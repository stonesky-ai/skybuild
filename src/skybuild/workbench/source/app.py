"""The server: routes, caller and write refusals, the todo queue proxy page, and `main`.
"""
from __future__ import annotations

import ipaddress
import json
import math
import os
import sys
import threading
import time
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from . import hub as sv
from .settings import ENV_PREFIX

if TYPE_CHECKING:      # names used in annotations only (strings at runtime)
    from .board import Board
    from .cleanup import Cleanup
    from .settings import WebConfig
    from .sources.summaries import Summaries

HELP = """sessionview_web: one build-status source for the web page and terminal markdown.

  sessionview                    the web page, on 127.0.0.1:$SESSIONVIEW_WEB_PORT (default 18437)
  sessionview-console            the same state as terminal markdown (--markdown)
  sessionview-console --watch N  re-rendered every N seconds

Settings (environment only, ADR-0002) and the package map: docs/dev/sessionview.md."""


# ---------------------------------------------------------------- the state, built beside the requests

class StateKeeper:
    """The state document the page is served: built on its own thread, never inside a request.

    Building the state reads git, /proc, the journals and the other boxes, and took 8 to 11 seconds
    on 2026-10-06. Built inside the request, every refresh waited that long and a newly opened page
    stayed blank for it. `run` rebuilds every `interval` seconds while a page is asking and `state`
    answers at once with the last document. A request waits (at most `wait_seconds`) only when no
    document exists yet, when the last one is older than `idle_refreshes` intervals (nobody was
    looking, so `run` had stopped), or when `kick` says a button was just pressed and the page must
    see its effect. A build that raises keeps the last document, and says so in `build_error`.
    """

    def __init__(self, build: Callable[[], dict], interval: float, *, wait_seconds: float,
                 idle_refreshes: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._build = build
        self.interval = max(1.0, interval)
        self.wait_seconds = wait_seconds
        self.idle_seconds = max(1.0, idle_refreshes) * self.interval
        self._clock = clock
        self._cond = threading.Condition()
        self._state: dict | None = None
        self._built_at = 0.0      # when the last finished build started (clock time)
        self._started = 0         # builds started
        self._done = 0            # the last build finished, by its start number
        self._need = 0            # a request must not be answered from a build older than this one
        self._asked_at = clock()  # the last time a page asked
        self._error = ""
        self._stopped = False

    def build_once(self) -> None:
        with self._cond:
            self._started += 1
            mine, began = self._started, self._clock()
        try:
            state, error = self._build(), ""
        except Exception as exc:  # noqa: BLE001 - the page says the rebuild failed and keeps the last state
            state, error = None, type(exc).__name__
        with self._cond:
            if state is not None:
                self._state, self._built_at = state, began
            self._error = error
            self._done = mine
            self._cond.notify_all()

    def run(self) -> None:
        """Rebuild while a page is asking; sleep, costing nothing, while none is."""
        while True:
            began = self._clock()
            self.build_once()
            with self._cond:
                self._cond.wait_for(lambda: self._stopped or self._need > self._done,
                                    timeout=max(0.0, self.interval - (self._clock() - began)))
                self._cond.wait_for(lambda: self._stopped or self._need > self._done
                                    or self._clock() - self._asked_at < self.idle_seconds)
                if self._stopped:
                    return

    def stop(self) -> None:
        with self._cond:
            self._stopped = True
            self._cond.notify_all()

    def kick(self) -> None:
        """A button was pressed: the next answer comes from a build that starts after now."""
        with self._cond:
            self._need = self._started + 1
            self._cond.notify_all()

    def state(self) -> dict:
        with self._cond:
            now = self._clock()
            self._asked_at = now
            if self._state is not None and now - self._built_at > self.idle_seconds:
                self._need = max(self._need, self._started + (0 if self._started > self._done else 1))
            self._cond.notify_all()
            self._cond.wait_for(lambda: self._state is not None and self._done >= self._need,
                                timeout=self.wait_seconds)
            if self._state is None:
                return {"sections": {}, "build_error": self._error or "the first state is still being built"}
            return {**self._state, "build_error": self._error} if self._error else self._state


# ---------------------------------------------------------------- the server

def allowed_hosts(port: int) -> set[str]:
    return {f"{sv.LOOPBACK}:{port}", f"localhost:{port}", f"[::1]:{port}"}


def caller_refusal(client_host: str, headers: Mapping[str, str], port: int) -> str:
    """Why this caller is refused, or "" when it may be answered.

    The caller must be on this machine, and the Host header must name this
    server: a browser pointed here by a rebinding DNS name sends that name.
    """
    try:
        address = ipaddress.ip_address(client_host)
    except ValueError:
        return f"caller address {client_host!r} is unreadable"
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    if not address.is_loopback:
        return f"caller {client_host} is not on this machine"
    host = (headers.get("Host") or "").strip().lower()
    if host not in allowed_hosts(port):
        return f"Host {host or '(none)'!r} does not name this server"
    return ""


#: The todo service's people page (`scripts/todo_service/app.py`, GET /queue.html), fetched for
#: the viewer with this box's token: a browser cannot send the bearer header a link needs.
TODO_URL_SETTING = ENV_PREFIX + "WEB_TODO_SERVICE_URL"
TODO_TOKEN_SETTING = ENV_PREFIX + "WEB_TODO_TOKEN_FILE"
TODO_TIMEOUT_SETTING = ENV_PREFIX + "WEB_TODO_TIMEOUT_SECONDS"
QUEUE_PAGE_CSP = "default-src 'none'"


def todo_queue_page(environ: Mapping[str, str], transport: Callable | None = None) -> tuple[int, str]:
    """`(200, the page)`, or `(503 | 502, why)` as plain words. The token is never in the answer."""
    todo_client = sv._load("sessionview_web_todo_client", "todo_service/client.py")
    for setting in (TODO_URL_SETTING, TODO_TOKEN_SETTING, TODO_TIMEOUT_SETTING):
        if not environ.get(setting, "").strip():
            return 503, f"{setting} is not set"
    try:
        timeout = float(environ[TODO_TIMEOUT_SETTING])
        if not timeout > 0:
            raise ValueError
    except ValueError:
        return 503, f"{TODO_TIMEOUT_SETTING} is not a positive number"
    try:
        base = todo_client.check_url(environ[TODO_URL_SETTING])
        token = todo_client.read_token(environ[TODO_TOKEN_SETTING].strip())
    except todo_client.ClientConfigError as exc:
        return 503, f"the todo service is not configured: {exc}"
    try:
        status, body = (transport or todo_client.http_transport)(
            "GET", base + "/queue.html", {"Authorization": f"Bearer {token}"}, None, timeout)
    except OSError as exc:
        return 502, f"the todo service is unreachable: {type(exc).__name__}"
    if status != 200:
        return 502, f"the todo service answered {status}"
    return 200, body.decode("utf-8", "replace")


def make_server(wcfg: WebConfig, board: Board | None = None,
                state_fn: Callable[[], dict] | None = None,
                cleanup: Cleanup | None = None,
                summaries: Summaries | None = None,
                queue_page: Callable[[], tuple[int, str]] | None = None,
                alarm_ack: Callable[[object, str], tuple[int, dict]] | None = None,
                on_write: Callable[[], None] | None = None) -> ThreadingHTTPServer:
    """A server on the loopback interface only. It is not started.

    `on_write` is called after every write the server accepted (a `StateKeeper.kick`: the page reads
    the state again at once and must see what the write changed).
    """
    queue_page = queue_page or (lambda: todo_queue_page(os.environ))
    board = board or (None if state_fn else sv.Board(wcfg))
    get_state = state_fn or board.state
    cleanup = cleanup or (board.cleanup if board is not None else None)
    summaries = summaries or (board.summaries if board is not None else None)
    alarm_ack = alarm_ack or (board.todo_reader.ack_alarm if board is not None else None)
    routes = ({"/api/cleanup/plan", "/api/cleanup/run"} if cleanup is not None else set()) | \
        ({"/api/summary"} if summaries is not None else set()) | \
        ({ALARM_ACK_ROUTE} if alarm_ack is not None else set())

    class Handler(BaseHTTPRequestHandler):
        server_version = "sessionview-web"

        def log_message(self, fmt: str, *args) -> None:
            return

        def _send(self, code: int, content_type: str, body: bytes,
                  extra: Mapping[str, str] | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            for name, value in (extra or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, payload: dict) -> None:
            self._send(code, "application/json; charset=utf-8", json.dumps(payload).encode("utf-8"))

        def _refused(self) -> bool:
            why = caller_refusal(self.client_address[0], self.headers, wcfg.port)
            if why:
                self._json(403, {"refused": why})
                return True
            return False

        def do_GET(self) -> None:
            if self._refused():
                return
            path = urlparse(self.path).path
            # One page serves every view: /view/<name> is the same document, and its script
            # shows that view (an unknown name shows the default view, never an error page).
            if path == "/" or (path.startswith(VIEW_PREFIX) and sv.PAGE_NAME.fullmatch(path[len(VIEW_PREFIX):])):
                self._send(200, "text/html; charset=utf-8", sv.PAGE_HTML.encode("utf-8"),
                           {"Content-Security-Policy": sv.PAGE_CSP})
            elif path == "/api/state":
                self._json(200, get_state())
            elif path == "/queue.html":
                status, text = queue_page()
                if status == 200:
                    self._send(200, "text/html; charset=utf-8", text.encode("utf-8"),
                               {"Content-Security-Policy": QUEUE_PAGE_CSP})
                else:
                    self._json(status, {"error": text})
            else:
                self._json(404, {"error": "not found"})

        def _body(self) -> dict | None:
            """The JSON object posted, or None after answering the refusal."""
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                length = -1
            if not 0 <= length <= MAX_BODY_BYTES:
                self._json(413, {"refused": f"a body of 0 to {MAX_BODY_BYTES} bytes is required"})
                return None
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                body = None
            if not isinstance(body, dict):
                self._json(400, {"refused": "the body must be a JSON object"})
                return None
            return body

        def do_POST(self) -> None:
            if self._refused():
                return
            why = write_refusal(self.headers, wcfg.port)
            if why:
                self._json(403, {"refused": why})
                return
            path = urlparse(self.path).path
            if path not in routes:
                self._json(404, {"error": "not found"})
                return
            body = self._body()
            if body is None:
                return
            if path == "/api/summary":
                # Starts an ask and answers at once; it writes nothing.
                code, payload = summaries.request(body.get("topic"), time.time())
            elif path == ALARM_ACK_ROUTE:
                # The one write that leaves this box: an ack, sent on to the todo service with this
                # box's token, which never appears in the answer.
                code, payload = alarm_ack(body.get("id"), f"sessionview-web@{os.uname().nodename.split('.')[0]}")
            elif path == "/api/cleanup/plan":
                code, payload = 200, cleanup.plan(time.time())
            else:
                code, payload = cleanup.run(body.get("plan"), body.get("items"), time.time())
            if on_write is not None:
                on_write()
            self._json(code, payload)

    return ThreadingHTTPServer((sv.LOOPBACK, wcfg.port), Handler)


#: A cleanup request is a plan id and a list of item ids, a summary request one
#: topic name, an ack one alarm id; nothing bigger is read.
MAX_BODY_BYTES = 65536
#: The page's views: /view/<name> serves the page, whose script shows that view.
VIEW_PREFIX = "/view/"
#: The one write the page sends to the todo service: an alarm's ack.
ALARM_ACK_ROUTE = "/api/alarms/ack"


def write_refusal(headers: Mapping[str, str], port: int) -> str:
    """Why a write is refused, or "". Checked after `caller_refusal`.

    Another site's page in the owner's browser can still send a request to
    127.0.0.1. A JSON body cannot be sent cross-site without a preflight this
    server never answers, and the browser's Origin and Sec-Fetch-Site headers
    name where the request came from; any of the three refuses it.
    """
    ctype = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if ctype != "application/json":
        return "a write must be sent as application/json"
    origin = headers.get("Origin")
    if origin is not None and origin.strip().lower() not in {f"http://{h}" for h in allowed_hosts(port)}:
        return f"Origin {origin!r} is not this page"
    site = headers.get("Sec-Fetch-Site")
    if site is not None and site.strip().lower() not in ("same-origin", "none"):
        return f"Sec-Fetch-Site {site!r}: only this page may write"
    return ""


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and (argv[0] in ("-h", "--help") or
                 argv == ["--markdown", "--help"] or argv == ["--markdown", "-h"]):
        print(HELP)
        print("Usage: sessionview [--help]; sessionview-console [--watch SECONDS]")
        return 0
    markdown = bool(argv and argv[0] == "--markdown")
    watch: float | None = None
    if markdown:
        rest = argv[1:]
        if rest:
            if len(rest) != 2 or rest[0] != "--watch":
                print("REFUSED: use --markdown [--watch SECONDS]", file=sys.stderr)
                return 2
            try:
                watch = float(rest[1])
            except ValueError:
                watch = 0
            if not math.isfinite(watch) or watch <= 0:
                print("REFUSED: --watch needs a positive number of seconds", file=sys.stderr)
                return 2
    elif argv:
        print("REFUSED: use --markdown [--watch SECONDS]", file=sys.stderr)
        return 2
    try:
        wcfg = sv.resolve_config()
    except sv.Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    board = sv.Board(wcfg)
    if markdown:
        try:
            while True:
                print(sv.render_markdown(board.state()), end="", flush=True)
                if watch is None:
                    return 0
                time.sleep(watch)
        except KeyboardInterrupt:
            return 0
        except (OSError, ValueError) as exc:
            print(f"REFUSED: cannot render the board: {exc}", file=sys.stderr)
            return 1
    keeper = StateKeeper(board.state, wcfg.sv.interval, wait_seconds=wcfg.state_wait_seconds,
                         idle_refreshes=wcfg.state_idle_refreshes)
    try:
        httpd = make_server(wcfg, board=board, state_fn=keeper.state, on_write=keeper.kick)
    except OSError as exc:
        print(f"REFUSED: cannot listen on {sv.LOOPBACK}:{wcfg.port}: {exc.strerror}", file=sys.stderr)
        return 1
    stop = threading.Event()

    def keep_sampling() -> None:
        # The prediction needs history whether or not a page is open.
        while not stop.wait(wcfg.disk_sample_seconds):
            board.sample_disk()

    board.sample_disk()
    threading.Thread(target=keep_sampling, name="disk-sampler", daemon=True).start()
    threading.Thread(target=keeper.run, name="state-keeper", daemon=True).start()
    print(f"sessionview_web http://{sv.LOOPBACK}:{wcfg.port}/", flush=True)
    if wcfg.summary is not None:
        print(f"summaries on request: {wcfg.summary.model} at {wcfg.summary.host}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        stop.set()
        keeper.stop()
        httpd.server_close()
    return 0
