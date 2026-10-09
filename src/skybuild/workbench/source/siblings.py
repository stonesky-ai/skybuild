"""The sibling scripts the page reads through, loaded by path from scripts/ so any cwd works.

Each is the owning script's own logic, never a copy: `claims.py` for who holds which row,
`agents/claude_accounts.py` for the accounts' usage, the gates view for what the integrator is
doing, and the batch history for the trunk's past batches.
"""
from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path

#: scripts/: this package's parent, where the sibling scripts and the entry points live.
SCRIPTS = Path(__file__).resolve().parent.parent


#: One thread loads a sibling at a time: `sys.modules` holds a module before its body has run, so a
#: second thread (the todo probe against a request) would otherwise be handed a half-built one.
_LOAD_LOCK = threading.RLock()


def _load(name: str, relative: str):
    """A sibling script as a module, loaded by path so any cwd works; a caller never sees it half-loaded."""
    with _LOAD_LOCK:
        if name in sys.modules:
            return sys.modules[name]
        spec = importlib.util.spec_from_file_location(name, SCRIPTS / relative)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name, None)
            raise
        return module


#: `scripts/claims.py`'s own claimed/unlanded signal — the In-process box's source
#: of truth, never a forked copy of its claim-parsing logic.
claims = _load("sessionview_web_claims", "claims.py")
#: The Claude accounts and their usage readings, as `agents/claude_accounts.py` reads them.
accounts = _load("claude_accounts", "agents/claude_accounts.py")

#: What the integrator is doing now (batch, seams, stage, suites, ETA): read-only.
#: Previous Batch Stats: the past batches of the trunk, read from its first-parent log.
from . import batch_history  # noqa: F401
from .gates import hub as integrator_run  # noqa: F401
