"""Install the shared workbench views and copied status page."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response

from . import fleet_inventory, navigation
from ..ledger import build_manifest
from .source import page

ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT.parents[1]
STATIC = ROOT / "static"
HEADERS = {
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


def _revision() -> str:
    digest = hashlib.sha256()
    paths = [ROOT / "web.py", ROOT / "workbench" / "web.py", ROOT / "workbench" / "navigation.py",
             ROOT / "workbench" / "fleet_inventory.py"]
    paths.extend(path for path in STATIC.iterdir() if path.is_file())
    paths.extend(path for path in (ROOT / "workbench" / "source").rglob("*") if path.is_file() and "__pycache__" not in path.parts)
    for path in sorted(set(paths)):
        digest.update(str(path.relative_to(ROOT)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def _dev_script(revision: str) -> str:
    return f'<script src="/workbench/dev/reload.js?revision={revision}" defer></script>'


def install_workbench(app: FastAPI, *, dev_reload: bool = False) -> None:
    """Mount task UI and copied status views. Both use the existing task API."""

    @app.get("/workbench", include_in_schema=False)
    def workbench_home() -> HTMLResponse:
        body = (STATIC / "workbench.html").read_text(encoding="utf-8")
        body = body.replace("<!--WORKBENCH_NAV-->", navigation.sidebar("workbench"))
        if dev_reload:
            body = body.replace("</body>", _dev_script(_revision()) + "</body>")
        return HTMLResponse(body, headers={
            **HEADERS,
            "Content-Security-Policy": "default-src 'self'; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'",
        })

    for route, filename, media_type in (
        ("/workbench/assets/workbench.js", "workbench.js", "text/javascript"),
        ("/workbench/assets/workbench.css", "workbench.css", "text/css"),
        ("/workbench/assets/workbench-shell.css", "workbench-shell.css", "text/css"),
        ("/workbench/assets/tasks.js", "tasks.js", "text/javascript"),
        ("/workbench/assets/tasks.css", "tasks.css", "text/css"),
        ("/workbench/assets/milestones.css", "milestones.css", "text/css"),
        ("/workbench/assets/milestone-roadmap.png", "milestone-roadmap.png", "image/png"),
        ("/workbench/assets/project-dependency-design.md", "project-dependency-design.md", "text/markdown; charset=utf-8"),
    ):
        app.add_api_route(route, _asset_handler(filename, media_type), methods=["GET"], include_in_schema=False)

    @app.get("/workbench/tasks", include_in_schema=False)
    def tasks_page() -> HTMLResponse:
        body = (STATIC / "tasks.html").read_text(encoding="utf-8")
        body = body.replace("<!--WORKBENCH_NAV-->", navigation.sidebar("tasks"))
        if dev_reload:
            body = body.replace('<html lang="en">', '<html lang="en" data-skybuild-preview="true">')
            body = body.replace("</body>", _dev_script(_revision()) + "</body>")
        return HTMLResponse(body, headers={
            **HEADERS,
            "Content-Security-Policy": "default-src 'self'; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'",
        })

    @app.get("/workbench/milestones", include_in_schema=False)
    def milestones_page() -> HTMLResponse:
        body = (STATIC / "milestones.html").read_text(encoding="utf-8")
        body = body.replace("<!--WORKBENCH_NAV-->", navigation.sidebar("milestones"))
        if dev_reload:
            body = body.replace("</body>", _dev_script(_revision()) + "</body>")
        return HTMLResponse(body, headers={
            **HEADERS,
            "Content-Security-Policy": "default-src 'self'; img-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'",
        })

    @app.get("/workbench/views/{view_name}", include_in_schema=False)
    @app.get("/workbench/views", include_in_schema=False)
    def status_view(view_name: str = "all") -> HTMLResponse:
        if view_name not in {item["key"] for item in page.PAGES}:
            raise HTTPException(404)
        body = page.PAGE_HTML
        body = body.replace("<!--WORKBENCH_NAV-->", navigation.sidebar(view_name))
        body = re.sub(r'<nav id="rail"[\s\S]*?</nav>', '', body, count=1)
        if dev_reload:
            body = body.replace("<body>", '<body data-fleet-inventory="enabled">')
            body = body.replace("</body>", _dev_script(_revision()) + "</body>")
        policy = page.PAGE_CSP.replace("style-src ", "style-src 'self' ")
        if dev_reload:
            policy = policy.replace("script-src ", "script-src 'self' ")
        return HTMLResponse(body, headers={**HEADERS, "Content-Security-Policy": policy})

    @app.get("/workbench/api/state", include_in_schema=False)
    def workbench_state() -> JSONResponse:
        return JSONResponse({
            "repo": "SkyBuild · embedded status preview",
            "generated_text": "Sample state · live SkyKeep collectors are not connected",
            "interval_ms": 60_000,
            "sections": {"preview": {
                "status": "SkyKeep tools page copied into SkyBuild",
                "message": "This is the embedded UI preview. Live SkyKeep data and actions are not connected yet.",
                "source": "src/skybuild/workbench/source",
            }},
        }, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @app.get("/workbench/queue.html", include_in_schema=False)
    def workbench_queue() -> HTMLResponse:
        return HTMLResponse(
            "<!doctype html><html lang='en'><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
            "<title>Todo queue preview</title><body><main><h1>Todo queue</h1><p>The SkyKeep todo service is not connected to this preview.</p>"
            "<p><a href='/workbench'>Return to Workbench</a></p></main></body></html>",
            headers={**HEADERS, "Content-Security-Policy": "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"},
        )

    if dev_reload:
        @app.get("/workbench/api/fleet", include_in_schema=False)
        def fleet_snapshot() -> JSONResponse:
            return JSONResponse(fleet_inventory.snapshot(), headers={"Cache-Control": "no-store"})

        @app.post("/workbench/api/fleet/refresh", include_in_schema=False)
        def fleet_refresh() -> JSONResponse:
            status, payload = fleet_inventory.refresh()
            headers = {"Cache-Control": "no-store"}
            if status == 429:
                headers["Retry-After"] = str(payload.get("retry_after", 10))
            return JSONResponse(payload, status_code=status, headers=headers)

        @app.get("/workbench/dev/tasks-preview.json", include_in_schema=False)
        def preview_tasks() -> JSONResponse:
            manifest = build_manifest([PROJECT_ROOT / "docs" / "design" / "mastertodo.md"])
            tasks = []
            for priority, record in enumerate(manifest["tasks"]):
                heading = re.match(r"^##\s+\S+\s+[—–-]\s+(.+?)\s*$", record["raw"].splitlines()[0])
                if not heading:
                    continue
                tasks.append({
                    "task_id": record["task_id"], "title": heading.group(1),
                    "description": record["raw"], "status": record["status"],
                    "phase": "mastertodo preview", "priority": priority,
                    "next_action": "Read-only preview from mastertodo.md; no live task action.",
                    "blocker": "", "responsible": "fake-preview", "assignee": None,
                    "dependencies": [], "acceptance_criteria": [], "architecture_refs": [],
                    "revision": 1, "created_at": "mastertodo.md", "updated_at": "mastertodo.md",
                    "metadata": {"fake": True, "source": "docs/design/mastertodo.md",
                                 "waiting_on": "TBD", "will_enable": "TBD"}, "fake": True,
                })
            return JSONResponse(tasks, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

        @app.get("/workbench/dev/revision", include_in_schema=False)
        def dev_revision() -> JSONResponse:
            return JSONResponse({"revision": _revision()}, headers={"Cache-Control": "no-store"})

        @app.get("/workbench/dev/reload.js", include_in_schema=False)
        def dev_reload_script(revision: str) -> HTMLResponse:
            source = """(()=>{const script=document.currentScript;const initial=new URL(script.src).searchParams.get('revision');setInterval(async()=>{try{const response=await fetch('/workbench/dev/revision',{cache:'no-store'});if(!response.ok)return;const current=(await response.json()).revision;if(initial&&current!==initial)location.reload()}catch(_){}} ,1000)})();"""
            return Response(source, media_type="text/javascript", headers={
                "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            })


def _asset_handler(filename: str, media_type: str):
    def handler() -> Response:
        return Response((STATIC / filename).read_bytes(), media_type=media_type, headers=HEADERS)
    return handler
