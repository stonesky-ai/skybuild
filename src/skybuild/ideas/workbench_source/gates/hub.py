"""The one namespace of the gates view (what the integrator is doing): every module's names and `sv`.

Same rule as the page's hub: modules call one another through `sv`, so `monkeypatch.setattr(mod, "_cwd_of", ...)`
on `scripts/sessionview_integrator.py` (which forwards here) reaches every caller. `remote_source` is the
whole view as one program, for the probe that runs it on another box.
"""
import sys

from . import (
    batch,
    boxes,
    clock,
    fleet,
    fleet_scan,
    gate,
    gate_log,
    keeper,
    kept_warnings,
    lane_logs,
    processes,
    steps,
)

_ORDER = (processes, lane_logs, gate, clock, batch, kept_warnings, gate_log, steps, boxes, fleet_scan, keeper, fleet,)

for _module in _ORDER:
    for _name, _value in vars(_module).items():
        if not _name.startswith("__") and _name not in ("sv", "hub"):
            globals()[_name] = _value
del _module, _name, _value

#: This module: what every module of the package calls its siblings through.
sv = sys.modules[__name__]



import re as _re
from pathlib import Path as _Path

_RELATIVE_IMPORT = _re.compile(r"^from \.+\S* import ")


def remote_source() -> str:
    """The whole gates view as one program, for `python3 -` on another box (the ssh probe pipes it).

    The modules are joined in load order with their relative imports dropped: in one file every
    name is a global, so `sv` (the running program itself) resolves exactly as the hub does here.
    """
    here = _Path(__file__).resolve().parent
    parts = ["from __future__ import annotations", "import sys", "sv = sys.modules[__name__]"]
    for name in _ORDER:
        text = (here / (name.__name__.rsplit(".", 1)[-1] + ".py")).read_text(encoding="utf-8")
        kept = [line for line in text.splitlines()
                if not _RELATIVE_IMPORT.match(line) and not line.startswith("from __future__ import")]
        parts.append("\n".join(kept))
    return "\n".join(parts) + "\n"


#: The Integration now plan: every step of a run with its history. Imported last and kept out of `_ORDER`
#: (and so out of `remote_source`): it reads this box's own files and never runs on another box.
from . import run_plan  # noqa: F401
