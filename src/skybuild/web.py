"""Install the bounded browser workbench without accessing the Store."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse


HEADERS = {
    "Content-Security-Policy": "default-src 'self'; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
}


def _asset(path: Path, media_type: str, *, preview: bool = False):
    def handler():
        if preview and media_type == "text/html":
            body = path.read_text(encoding="utf-8")
            body = body.replace("</head>", '<link rel="stylesheet" href="/workbench/assets/workbench-shell.css"></head>')
            body = body.replace("<body>", '<body><nav class="workbench-shell-nav" aria-label="SkyBuild sections"><a href="/workbench" aria-current="page">Task workbench</a><a href="/workbench/marshalls">Marshalls</a></nav>')
            return HTMLResponse(body, headers=HEADERS)
        return FileResponse(path, media_type=media_type, headers=HEADERS)

    return handler


def install_workbench(app: FastAPI, *, dev_reload: bool = False) -> None:
    """Expose three fixed public files; authenticated data uses the existing API."""
    directory = Path(__file__).with_name("static")
    for route, filename, media_type in (
        ("/workbench", "workbench.html", "text/html"),
        ("/workbench/assets/workbench.js", "workbench.js", "text/javascript"),
        ("/workbench/assets/workbench.css", "workbench.css", "text/css"),
    ):
        app.add_api_route(route, _asset(directory / filename, media_type, preview=dev_reload), methods=["GET"], include_in_schema=False)

    if dev_reload:
        from .workbench.preview import install_marshalls

        install_marshalls(app)
