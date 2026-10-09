"""Read-only SkyKeep tools preview mounted inside the SkyBuild workbench."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from .workbench_source import page

IDEAS_BASE = "/ideas"
WORKBENCH_PREVIEW_STATE = {
    "repo": "SkyBuild · embedded Ideas preview",
    "generated_text": "Sample state · live SkyKeep collectors are not connected",
    "interval_ms": 60_000,
    "sections": {
        "preview": {
            "status": "SkyKeep tools page copied into SkyBuild",
            "message": "This is the embedded UI preview. Live SkyKeep data and actions are not connected yet.",
            "source": "src/skybuild/ideas/workbench_source",
        },
    },
}


def install_ideas_workbench(app: FastAPI) -> None:
    """Mount the copied page and static preview data without starting its collectors."""

    @app.get(IDEAS_BASE, include_in_schema=False)
    @app.get(IDEAS_BASE + "/", include_in_schema=False)
    @app.get(IDEAS_BASE + "/view/{view_name}", include_in_schema=False)
    def workbench_page(view_name: str = "") -> HTMLResponse:
        policy = page.PAGE_CSP.replace("style-src ", "style-src 'self' ")
        return HTMLResponse(page.PAGE_HTML, headers={
            "Content-Security-Policy": policy,
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
        })

    @app.get(IDEAS_BASE + "/api/state", include_in_schema=False)
    def workbench_state() -> JSONResponse:
        return JSONResponse(WORKBENCH_PREVIEW_STATE, headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        })

    @app.get(IDEAS_BASE + "/queue.html", include_in_schema=False)
    def workbench_queue() -> HTMLResponse:
        return HTMLResponse(
            "<!doctype html><html lang='en'><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            "<title>Todo queue preview</title><body><main><h1>Todo queue</h1>"
            "<p>The SkyKeep todo service is not connected to this preview.</p>"
            "<p><a href='/ideas'>Return to Ideas</a></p></main></body></html>",
            headers={
                "Content-Security-Policy": "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
            },
        )
