"""Explicit local-only development preview; never installed by the service."""
from fastapi import FastAPI

from .web import install_workbench

app = FastAPI(title="SkyBuild local Workbench preview")
install_workbench(app, dev_reload=True)
