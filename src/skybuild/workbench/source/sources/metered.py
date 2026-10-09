"""The metered-endpoint alarm: when nothing needs the paid endpoint, say so loudly.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from .. import hub as sv

if TYPE_CHECKING:      # names used in annotations only (strings at runtime)
    from ..sections.work import Runner


def metered_alarm(cfg, seams: dict, integrator: list, lanes: list[str],
                  runner: Runner | None = None) -> dict | None:
    """The banner telling the owner to shut a metered endpoint down, or None.

    None whenever no metered endpoint is named: the page then has no banner at
    all, which is the state of every box that pays for nothing. Named, the
    banner is armed, and it FIRES once the metered box has no work left that
    needs it: no seam ready to land, none pushed and awaiting a merge, and no
    lane running. Each condition still outstanding is reported, so an armed
    banner that is not firing says what it is waiting for rather than going
    quiet.

    What does NOT silence it: pointing the lane configuration at another
    endpoint. Switching the lanes away is the moment the metered box becomes
    pure cost, so it makes the banner MORE urgent, never less. Only the
    acknowledgement file says the box is actually off, because only a person
    can know that — nothing here can see a machine that has stopped answering
    from a machine that was never asked. Unset the name to retire the banner.
    """
    if not cfg.metered_name:
        return None
    if cfg.metered_ack and cfg.metered_ack.exists():
        return None
    endpoint = sv.gate_endpoint(cfg.gate_conf)
    in_use = bool(endpoint) and bool(cfg.metered_endpoint) and endpoint == cfg.metered_endpoint
    outstanding: list[str] = []
    # A seam a reviewer HELD is waiting for a person, and no lane will run for
    # it until someone acts. Counting it would pin the banner shut forever:
    # measured 2026-09-30, three of the three seams the page called "READY to
    # land" were all HOLD, so the banner could never have fired and the pod
    # would have billed all night with nobody told.
    sv_cfg = getattr(cfg, "sv", None)
    standings = sv.review_standings(cfg.reviews_dir, getattr(sv_cfg, "repo", None),
                                 getattr(sv_cfg, "remote", None), runner)
    held = {row for row, (verdict, _why) in standings.items() if verdict in sv.HELD_VERDICTS}
    superseded = {row: why for row, (_verdict, why) in standings.items() if why}
    ready = [row for row in (seams.get("ready") or []) if str(row.get("id")) not in held]
    waiting = [row for row in integrator if str(row.get("id")) not in held]
    if ready:
        outstanding.append(f"{len(ready)} seam(s) READY to land: "
                           + ", ".join(str(row.get("id")) for row in ready[:6]))
    if waiting:
        outstanding.append(f"{len(waiting)} seam(s) pushed and not yet merged")
    if lanes:
        outstanding.append(f"{len(lanes)} lane process(es) running")
    reason = ("no seam is ready to land, none is waiting to be merged, and no lane is running: "
              f"{cfg.metered_name} is costing money and doing nothing")
    if not in_use:
        reason = (f"the lanes have been switched away from {cfg.metered_name}"
                  + (f" to {endpoint}" if endpoint else "")
                  + f", so {cfg.metered_name} is now pure cost: nothing will use it again")
    ack = f"clear this by running: touch {cfg.metered_ack}" if cfg.metered_ack else ""
    return {
        "name": cfg.metered_name,
        "endpoint": endpoint,
        "in_use": in_use,
        "fire": not outstanding,
        "headline": f"TURN OFF {cfg.metered_name} NOW",
        "reason": reason,
        # Nothing here can shut a metered box down: the endpoint credential
        # authenticates to the model server inside the pod, not to the account
        # that bills for it. So the banner carries the manual step verbatim.
        "howto": cfg.metered_howto or "",
        "ack": ack,
        "outstanding": outstanding,
        "held": sorted(held),
        "superseded": superseded,
        "lanes": lanes[:6],
    }


def _optional_number(env: Mapping[str, str], var: str, percent: bool = False) -> float | None:
    """A positive number from `var`, None when unset; anything else refuses."""
    raw = env.get(var, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        raise sv.Refused(f"{var}={raw!r} is not a number") from None
    if not 0 < value < float("inf") or (percent and value > 100):
        raise sv.Refused(f"{var}={raw!r} must be a positive number" + (" of at most 100" if percent else ""))
    return value
