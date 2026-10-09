"""sessionview_web: the build-status page as a package (docs/dev/sessionview.md maps it).

`scripts/sessionview_web.py`, beside this package, is its command and its old name: that file
forwards every attribute to `hub`, the one namespace every module of the package shares, so the
systemd unit, the tests and the sibling scripts that load the old name keep working unchanged.
"""
import sys as _sys
from pathlib import Path as _Path

#: scripts/: the sibling scripts (`endpoint_shape`, `claims` ...) are imported by name from there.
_SCRIPTS = str(_Path(__file__).resolve().parent.parent)
if _SCRIPTS not in _sys.path:
    _sys.path.insert(0, _SCRIPTS)


def __getattr__(name: str):
    """`import sessionview_web; sessionview_web.Board` reads the hub, like the command file does.

    While the hub is still loading (it has no `sv` yet) there is nothing to forward, and saying
    so lets `from . import <module>` inside it fall through to the import system.
    """
    hub = _sys.modules.get(__name__ + ".hub")
    if hub is None or "sv" not in vars(hub):
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(hub, name)


from . import (
    hub as _hub,  # noqa: F401  (importing the package loads the page, as importing the old file did)
)
