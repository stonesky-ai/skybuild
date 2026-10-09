"""The one namespace of the page: every module's names, in load order, and `sv`, this module itself.

The modules call one another through `sv` (`sv.read_seams(...)`), never by a bound import, so a test that
replaces one collector on this module (`monkeypatch.setattr(web.sv, "iter_processes", ...)`) replaces it
for every caller. A name a module needs while it loads (a base class, a default, a constant in a table)
is imported directly instead. The entry points beside the package (`scripts/sessionview_web.py`,
`sessionview_integrator.py`, `sessionview_batch_history.py`) forward every attribute here, so each old
name keeps working for the systemd unit, the tests and the sibling scripts.
"""
import sys

from . import (
    app,
    board,
    cleanup,
    frame,
    legacy_page,
    markdown,
    page,
    panels,
    settings,
    siblings,
    terminal,
    util,
)
from .sections import (
    census,
    help,  # noqa: A004, RUF100 - the module sections/help.py, not the builtin
    questions,
    work,
)
from .sources import (
    barriers,
    codex_loop,
    critical_path,
    disk,
    flow,
    grok,
    health,
    lanes,
    lease,
    loops_memory,
    metered,
    ollama,
    overseer,
    reviews,
    seam_status,
    seams,
    sessions,
    subagents,
    summaries,
    todo_file,
    todo_service,
    todo_watch,
    worktrees,
)

_ORDER = (siblings, settings, util, sessions, lanes, ollama, codex_loop, seams, worktrees, subagents, todo_file, terminal, frame, grok, panels, legacy_page, reviews, seam_status, metered, summaries, work, census, questions, lease, disk, loops_memory, barriers, cleanup, critical_path, overseer, board, page, markdown, app, todo_watch, todo_service, health, flow, help,)

for _module in _ORDER:
    for _name, _value in vars(_module).items():
        if not _name.startswith("__") and _name not in ("sv", "hub"):
            globals()[_name] = _value
del _module, _name, _value

#: This module: what every module of the package calls its siblings through.
sv = sys.modules[__name__]
