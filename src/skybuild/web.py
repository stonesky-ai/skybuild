"""Install the public workbench and its Ideas page without accessing the Store."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from .ideas.workbench import install_ideas_workbench


HEADERS = {
    "Content-Security-Policy": "default-src 'self'; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
}


def _asset(path: Path, media_type: str):
    def handler() -> FileResponse:
        return FileResponse(path, media_type=media_type, headers=HEADERS)

    return handler


def install_workbench(app: FastAPI) -> None:
    """Expose fixed public assets and the Ideas preview; the task UI uses the existing API."""
    directory = Path(__file__).with_name("static")
    for route, filename, media_type in (
        ("/workbench", "workbench.html", "text/html"),
        ("/workbench/assets/workbench.js", "workbench.js", "text/javascript"),
        ("/workbench/assets/workbench.css", "workbench.css", "text/css"),
        ("/ideas/assets/workbench-shell.css", "workbench-shell.css", "text/css"),
    ):
        app.add_api_route(route, _asset(directory / filename, media_type), methods=["GET"], include_in_schema=False)
    install_ideas_workbench(app)
