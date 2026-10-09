"""The page's clock: every date-time in the zone `SESSIONVIEW_WEB_TIMEZONE` names, its abbreviation beside it.
"""
from __future__ import annotations

import os

#: The zone every date-time the page shows is in: a setting, `America/Chicago` unless the environment names another.
TIMEZONE_ENV = "SESSIONVIEW_WEB_TIMEZONE"
DEFAULT_TIMEZONE = "America/Chicago"


def zoned(stamp: float, fmt: str) -> str:
    """`stamp` formatted in the page's zone, with the zone abbreviation (CST or CDT) beside it."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    zone = ZoneInfo(os.environ.get(TIMEZONE_ENV) or DEFAULT_TIMEZONE)
    return datetime.fromtimestamp(stamp, tz=zone).strftime(fmt + " %Z")


def _clock(stamp: float) -> str:
    return zoned(stamp, "%H:%M:%S")


def warning_stamp(value: float | None) -> str:
    """When a kept warning was first or last seen; empty for a warning that never carried the time."""
    return "" if not value else zoned(value, "%m-%d %H:%M:%S")
