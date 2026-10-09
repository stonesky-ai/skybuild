"""The legacy single page (`--serve`): the terminal frame in `#once`, refreshed panel by panel.
"""
from __future__ import annotations

import html
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from . import hub as sv

if TYPE_CHECKING:      # names used in annotations only (strings at runtime)
    from .settings import Config


_LOOPBACK = "127.0.0.1"


def render_page(cfg: Config, snap: dict[str, str]) -> str:
    """One HTML page. The terminal frame is in `#once`; the script refreshes every panel in place."""
    interval_ms = max(1, int(cfg.interval * 1000))

    def block(element_id: str, title: str, text: str) -> str:
        return (
            f"<section><h2>{html.escape(title)}</h2>"
            f"<pre id=\"{element_id}\">{sv.panel_html(element_id, text)}</pre></section>"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>sessionview</title>
<style>
body {{ margin: 0; background: #0f1419; color: #e7ecf1;
  font: 14px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
header {{ padding: 16px 20px; border-bottom: 1px solid #243040; }}
h1 {{ font-size: 18px; margin: 0 0 4px; }}
header p {{ margin: 0; color: #8ea0b5; }}
main {{ padding: 16px 20px 48px; }}
.trio {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }}
section {{ background: #1a2330; border: 1px solid #2c3a4d; border-radius: 8px;
  padding: 10px 12px; margin: 0 0 12px; min-width: 0; }}
h2 {{ font-size: 12px; margin: 0 0 8px; color: #8fb4d6; letter-spacing: .04em;
  text-transform: uppercase; }}
pre {{ margin: 0; white-space: pre-wrap; word-break: break-word; }}
.badge {{ border-radius: 3px; font-weight: 600; }}
.badge-running {{ background: #16361f; color: #7ee2a0; box-shadow: 0 0 0 2px #16361f; }}
.badge-ready {{ background: #102c46; color: #84c5ff; box-shadow: 0 0 0 2px #102c46; }}
.badge-idle {{ background: #243040; color: #9fb0c3; box-shadow: 0 0 0 2px #243040; }}
.badge-quiet, .badge-blocked {{ background: #3b2f10; color: #f3c65b; box-shadow: 0 0 0 2px #3b2f10; }}
.badge-stuck, .badge-stopped {{ background: #43171c; color: #ff9a9a; box-shadow: 0 0 0 2px #43171c; }}
</style>
</head>
<body>
<header>
<h1>sessionview</h1>
<p>{html.escape(str(cfg.repo))} · port {cfg.port} · refreshes every {interval_ms / 1000:.0f}s without reloading</p>
</header>
<main>
<div class="trio">
{block("claude", "Claude sub-agents", snap["claude"])}
{block("codex", "Codex", snap["codex"])}
{block("grok", "Grok", snap["grok"])}
</div>
{block("seams", "Seams", snap["seams"])}
{block("progress", "In-progress seam ETA", snap["progress"])}
{block("mastertodo", "MasterToDo", snap["mastertodo"])}
{block("once", "Terminal frame", snap["once"])}
</main>
<script>
const ids = ["claude", "codex", "grok", "seams", "progress", "mastertodo", "once"];
async function refresh() {{
  try {{
    const response = await fetch("/snapshot", {{cache: "no-store"}});
    if (!response.ok) return;
    const data = await response.json();
    const html = data.html || {{}};
    for (const id of ids) {{
      const el = document.getElementById(id);
      if (!el) continue;
      // The server escaped every panel before adding its badges.
      if (Object.prototype.hasOwnProperty.call(html, id)) el.innerHTML = data.html[id];
      else if (Object.prototype.hasOwnProperty.call(data, id)) el.textContent = data[id];
    }}
  }} catch (err) {{}}
}}
setInterval(refresh, {interval_ms});
</script>
</body>
</html>
"""


def legacy_make_server(cfg: Config) -> ThreadingHTTPServer:
    """A loopback server. Raises ValueError when no port was given."""
    if cfg.port is None or not 1 <= cfg.port <= 65535:
        raise ValueError("no port: --serve requires --port N (ADR-0002)")
    scanner = sv.TranscriptScanner()
    sampler = sv.OllamaSampler()
    codex = sv.CodexReader()
    lock = threading.Lock()

    def snapshot() -> dict[str, str]:
        with lock:
            return sv.page_snapshot(cfg, scanner, sampler, codex)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            return

        def _send(self, code: int, content_type: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8",
                           render_page(cfg, snapshot()).encode("utf-8"))
            elif path == "/snapshot":
                snap = snapshot()
                body = {**snap, "html": {key: sv.panel_html(key, text) for key, text in snap.items()}}
                self._send(200, "application/json; charset=utf-8",
                           json.dumps(body).encode("utf-8"))
            else:
                self._send(404, "text/plain; charset=utf-8", b"not found")

    return ThreadingHTTPServer((_LOOPBACK, cfg.port), Handler)


def serve(cfg: Config) -> int:
    if cfg.port is None:
        print("REFUSED: --serve requires --port N (ADR-0002: there is no default port)",
              file=sys.stderr)
        return 2
    try:
        httpd = legacy_make_server(cfg)
    except OSError as exc:
        print(f"REFUSED: cannot listen on {_LOOPBACK}:{cfg.port}: {exc.strerror}", file=sys.stderr)
        return 1
    print(f"sessionview http://{_LOOPBACK}:{cfg.port}/", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        httpd.server_close()
    return 0
