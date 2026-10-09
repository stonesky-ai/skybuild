"""Local-only Workbench preview with source and browser auto-reload enabled."""
from fastapi import FastAPI

from .web import install_workbench

app = FastAPI(title="SkyBuild Workbench Preview")
install_workbench(app, dev_reload=True)
