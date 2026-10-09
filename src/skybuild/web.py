"""Install public Workbench pages without accessing the Store."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import Response

from .workbench.web import install_workbench as install_workbench_pages


HEADERS = {
    "Content-Security-Policy": "default-src 'self'; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
}


def _asset(path: Path, media_type: str):
    def handler() -> Response:
        return Response(path.read_bytes(), media_type=media_type, headers=HEADERS)

    return handler


def install_workbench(app: FastAPI, *, dev_reload: bool = False) -> None:
    """Expose public task UI and copied status views without database access."""
    directory = Path(__file__).with_name("static")
    for route, filename, media_type in (("/workbench/assets/workbench.js", "workbench.js", "text/javascript"),
                                        ("/workbench/assets/workbench.css", "workbench.css", "text/css"),
                                        ("/workbench/assets/workbench-shell.css", "workbench-shell.css", "text/css")):
        app.add_api_route(route, _asset(directory / filename, media_type), methods=["GET"], include_in_schema=False)
    install_workbench_pages(app, dev_reload=dev_reload)
