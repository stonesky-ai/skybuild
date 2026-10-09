"""Warnings that stay: `warnings.json`, capped, written at most once a minute.
"""
from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path

from . import hub as sv

#: Warnings kept on disk; past this the oldest cleared ones go first.
WARNING_CAP = 500
#: A still-active warning's last-seen time is written at most this often.
WARNING_WRITE_SECONDS = 60.0


# ---------------------------------------------------------------- warnings that stay

def default_warning_file() -> Path:
    named = os.environ.get("SESSIONVIEW_WEB_WARNINGS_FILE", "").strip()
    return Path(named) if named else Path.home() / ".local" / "state" / "skykeep-sessionview" / "warnings.json"


class WarningLog:
    """Every warning this view has raised, kept on disk and never removed by a refresh.

    A warning is identified by its code and subject. It is ACTIVE while the
    latest read still raises it and CLEARED once a read does not; a cleared one
    stays listed with when it was first and last seen and when it cleared, so
    a flash between two refreshes can still be asked about later. The file is
    this view's own (never the repo's); a write that fails is said in the
    section, not raised.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_warning_file()
        self.lock = threading.Lock()
        self.entries: dict[str, dict] = {}
        self.error = ""
        self._written = 0.0
        # A file that exists but cannot be read is someone's record: never write over it.
        self._unreadable = False
        try:
            loaded = json.loads(self.path.read_text())
            if isinstance(loaded, dict) and isinstance(loaded.get("entries"), dict):
                self.entries = {k: v for k, v in loaded["entries"].items() if isinstance(v, dict)}
        except (FileNotFoundError, NotADirectoryError):
            pass                                    # no file there, so nothing to protect
        except (OSError, ValueError) as exc:
            self._unreadable = True
            self.error = (f"the warning file could not be read ({type(exc).__name__}): starting empty, the file is "
                          "left as it is and never written over; warnings are kept in memory only until it is "
                          "repaired or moved aside and the view restarted")

    def observe(self, warnings: Sequence[Mapping[str, str]], now: float) -> None:
        with self.lock:
            changed = False
            current = set()
            for w in warnings:
                key = f"{w.get('code', '?')}|{w.get('subject', '')}"
                current.add(key)
                entry = self.entries.get(key)
                text = str(w.get("text", ""))[:600]
                if entry is None:
                    self.entries[key] = {"code": w.get("code", "?"), "subject": w.get("subject", ""),
                                         "text": text, "first": now, "last": now, "seen": 1, "episodes": 1,
                                         "active": True, "cleared": None}
                    changed = True
                    continue
                if not entry.get("active"):
                    entry.update(active=True, cleared=None, episodes=int(entry.get("episodes", 1)) + 1)
                    changed = True
                if entry.get("text") != text:
                    entry["text"] = text
                    changed = True
                entry["last"] = now
                entry["seen"] = int(entry.get("seen", 0)) + 1
            for key, entry in self.entries.items():
                if key not in current and entry.get("active"):
                    entry.update(active=False, cleared=now)
                    changed = True
            if len(self.entries) > sv.WARNING_CAP:
                cleared = sorted((k for k, e in self.entries.items() if not e.get("active")),
                                 key=lambda k: self.entries[k].get("last", 0))
                for key in cleared[:len(self.entries) - sv.WARNING_CAP]:
                    del self.entries[key]
                changed = True
            if changed or (self.entries and now - self._written >= WARNING_WRITE_SECONDS):
                self._save(now)

    def _save(self, now: float) -> None:
        if self._unreadable:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps({"entries": self.entries}, indent=1))
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
            self._written = now
            if self.error.startswith("could not write"):
                self.error = ""
        except OSError as exc:
            self.error = f"could not write the warning file ({type(exc).__name__}): warnings are kept in memory only"

    def section(self) -> dict:
        with self.lock:
            rows = [{"state": "ACTIVE" if e.get("active") else "cleared", "what": e.get("code"),
                     "subject": e.get("subject"), "warning": e.get("text"), "first_seen": sv.warning_stamp(e.get("first")),
                     "last_seen": sv.warning_stamp(e.get("last")), "cleared_at": sv.warning_stamp(e.get("cleared")),
                     "times_raised": e.get("episodes", 1)}
                    for e in sorted(self.entries.values(), key=lambda e: (not e.get("active"), -(e.get("last") or 0)))]
            out = {"file": str(self.path), "active": sum(1 for r in rows if r["state"] == "ACTIVE"),
                   "kept": len(rows), "warnings": rows}
            if self.error:
                out["problem"] = self.error
            return out
