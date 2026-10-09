"""Read the configured overseer status, preserving unknown and stale states."""
from pathlib import Path

from .. import hub as sv


def overseer_section(path: Path | None, now: float, stale_minutes: float) -> dict:
    """The overseer's status file as one panel: NEEDS OWNER first, then a line per role.

    A file that is unset, missing, empty or unreadable is `unknown`, an old one
    `stale`; only a recent one is `fresh`. Nothing here reads as healthy by default.
    """
    def answer(state: str, detail: str, owner: list[str] = (), roles: list[str] = ()) -> dict:
        return {"state": state, "detail": detail, "needs_owner": list(owner), "roles": list(roles)}

    if path is None:
        return answer("unknown", f"{sv.ENV}OVERSEER_STATUS unset")
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        modified = path.stat().st_mtime
    except FileNotFoundError:
        return answer("unknown", f"status file missing: {path}")
    except OSError as exc:
        return answer("unknown", f"status file unreadable: {type(exc).__name__}")
    if not text.strip():
        return answer("unknown", "status file empty")
    owner: list[str] = []
    roles: list[str] = []
    in_block = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("NEEDS OWNER"):
            in_block = True
            inline = line[len("NEEDS OWNER"):].lstrip(":").strip()
            if inline:
                owner.append(inline)
        elif in_block and line.startswith("- "):
            owner.append(line[2:].strip())
        else:
            in_block = False
            if " | " in line and not line.startswith(("[", "#")):
                roles.append(line)
    age = max(0.0, now - modified) / 60
    if age > stale_minutes:
        return answer("stale", f"last written {age:.0f} min ago (stale after {stale_minutes:g})", owner, roles)
    return answer("fresh", f"last written {age:.0f} min ago", owner, roles)


