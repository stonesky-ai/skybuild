"""Mount only the explicitly selected local Dunsel development preview."""
from pathlib import Path
import subprocess

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from . import marshalls

STATIC = Path(__file__).resolve().parents[1] / "static"
HEADERS = {
    "Content-Security-Policy": "default-src 'self'; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


def install_marshalls(app: FastAPI) -> None:
    """Install a fixed local process utility, without tasks or service authority."""
    def require_local(request: Request, *, mutation: bool = False) -> None:
        try:
            marshalls.require_local_request(request, mutation=mutation)
        except (PermissionError, ValueError):
            raise HTTPException(403, detail="Local same-origin preview request required") from None

    @app.get("/workbench/marshalls", include_in_schema=False)
    def page(request: Request):
        require_local(request)
        return FileResponse(STATIC / "marshalls.html", headers=HEADERS)

    def asset(filename, media_type):
        def handler(request: Request):
            require_local(request)
            return FileResponse(STATIC / filename, media_type=media_type, headers=HEADERS)
        return handler

    for filename, media_type in (("marshalls.js", "text/javascript"),
                                 ("marshalls.css", "text/css"),
                                 ("workbench-shell.css", "text/css")):
        app.add_api_route("/workbench/assets/" + filename, asset(filename, media_type),
                          methods=["GET"], include_in_schema=False)

    @app.get("/workbench/api/marshalls/dunsel", include_in_schema=False)
    def status(request: Request):
        require_local(request)
        try:
            return JSONResponse(marshalls.snapshot(), headers=HEADERS)
        except (OSError, ValueError):
            raise HTTPException(503, detail="Dunsel status unavailable") from None

    @app.post("/workbench/api/marshalls/dunsel/{action}", include_in_schema=False)
    def control(action: str, request: Request):
        require_local(request, mutation=True)
        if action not in {"enable", "disable", "start", "graceful", "kill"}:
            raise HTTPException(404)
        try:
            if action in {"enable", "disable"}:
                enabled = action == "enable"
                marshalls.set_enabled(enabled)
                result = {"enabled": enabled}
            elif action == "start":
                result = marshalls.start()
            elif action == "graceful":
                result = marshalls.graceful_stop()
            else:
                result = marshalls.kill()
        except marshalls.MarshallConflict as error:
            raise HTTPException(409, detail=str(error)) from None
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            raise HTTPException(503, detail="Dunsel control unavailable") from None
        return JSONResponse(result, headers=HEADERS)
