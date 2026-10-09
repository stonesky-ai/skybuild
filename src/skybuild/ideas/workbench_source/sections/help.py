"""Settings and help: every setting the page reads and whether it is set, the keys, and the metrics
the page does not have yet with the row that would gather each.

A setting's value is never shown: a path or a URL may name a host the owner keeps private, and a
token file's name says where a credential lives. The page says "set" or "unset (default N)" only.
"""
from __future__ import annotations

from collections.abc import Mapping

from ..settings import DIALS, SLOT_MODELS_VAR
from ..terminal import ENV as ENV_PREFIX

#: Where the page, its settings and its package map are described.
SESSIONVIEW_DOC = "docs/dev/sessionview.md"

#: Settings outside the DIALS table, with their meaning (page order).
NAMED_SETTINGS: tuple[tuple[str, str], ...] = (
    (ENV_PREFIX + "PORT", "the loopback port the page listens on"),
    (ENV_PREFIX + "UNRESOLVED", "Unresolved.md, when it is not the repo's"),
    (ENV_PREFIX + "TRUNK_REFS", "a git for-each-ref pattern whose newest match is the trunk (unset: the checkout's upstream)"),
    (ENV_PREFIX + "ZPOOL", "the pool whose allocation the Disk box reads"),
    (ENV_PREFIX + "LOOP_UNITS", "the loop units to read, when not discovered by pattern"),
    (ENV_PREFIX + "LOOP_UNIT_GLOB", "the pattern the loop units are discovered by"),
    (ENV_PREFIX + "MEM_FLOOR_GIB", "the memory floor, when not the loops' own"),
    (ENV_PREFIX + "CLAUDE_USAGE", "the Claude usage file, when not the loops' own"),
    (ENV_PREFIX + "FLEET_BOXES", "the other boxes the Fleet and Boxes pages probe"),
    (ENV_PREFIX + "GATE_CONF", "the gate lane configuration the metered banner reads"),
    (ENV_PREFIX + "REVIEWS_DIR", "the code-review seat files"),
    (ENV_PREFIX + "METERED_NAME", "the metered endpoint's name (the banner)"),
    (ENV_PREFIX + "METERED_ENDPOINT", "the metered endpoint the lanes must leave"),
    (ENV_PREFIX + "METERED_ACK_FILE", "exists: the owner says the metered box is off"),
    (ENV_PREFIX + "METERED_HOWTO", "what to press to shut the metered box down"),
    (ENV_PREFIX + "SUMMARY_BASE_URL", "the summary model endpoint (off when unset)"),
    (ENV_PREFIX + "SUMMARY_MODEL", "the summary model"),
    (ENV_PREFIX + "SUMMARY_SECRET_FILE", "the summary endpoint's credential file"),
    (ENV_PREFIX + "TODO_SERVICE_URL", "the todo service's base URL (the Alarms, Queue and Integration pages)"),
    (ENV_PREFIX + "TODO_TOKEN_FILE", "this box's todo token file, mode 0600"),
    (ENV_PREFIX + "TODO_TIMEOUT_SECONDS", "the todo queue proxy page's timeout"),
    (ENV_PREFIX + "TODO_REACH_DIR", "the boxes' reach records"),
    (ENV_PREFIX + SLOT_MODELS_VAR, "the models the keeper slots run; set, open rows no slot can take are flagged"),
    ("SKYKEEP_TODO_HEALTH_URL", "the todo service's /health URL"),
    ("SKYKEEP_CLAUDE_ACCOUNTS", "the Claude accounts file"),
    ("SKYKEEP_CLAUDE_WEEKLY_STOP_PCT", "the weekly usage stop line"),
    ("SKYKEEP_CLAUDE_USAGE_MAX_AGE_MINUTES", "how old a usage reading may be"),
    ("SKYKEEP_TIMING_DATA_DIR", "the gate timing data the Critical path box reads"),
)

#: What the page cannot show yet, and the todo-service row that would gather it.
METRICS_NOT_YET_GATHERED: tuple[dict, ...] = (
    {"metric": "Every box the todo service heard from: when it last asked for work and whether it was given any",
     "todo": "METRICS-TODO-SERVICE-LISTS-EVERY-BOX-IT-HEARD-FROM",
     "feeds": "Home, the box tiles: a box that holds no claim, or went silent"},
    {"metric": "How long rows usually wait in each stage: median and worst over the last day",
     "todo": "METRICS-BUILD-LINE-STAGE-WAITS", "feeds": "Home, the build line: a third fact under each stage"},
    {"metric": "The stage counts over the last day",
     "todo": "METRICS-BUILD-LINE-COUNTS-OVER-TIME", "feeds": "Home, the build line: a trend under each count"},
)

#: The keys the page answers.
KEYS: tuple[dict, ...] = (
    {"key": "/", "does": "open the page jump list; type to filter, Enter to go, Escape to close"},
    {"key": "Enter or Space on a cut cell", "does": "open or close the cell's full text"},
    {"key": "Enter or Space on a column heading", "does": "sort the table by that column; again for the other way"},
    {"key": "p", "does": "pause or resume the refresh"},
    {"key": "d", "does": "switch the density (compact or comfortable)"},
)


def help_section(wcfg, environ: Mapping[str, str]) -> dict:
    """The Settings and help section: set or unset per setting (never a value), the keys, the metrics owed."""
    settings = []
    for name, (default, meaning) in DIALS.items():
        var = ENV_PREFIX + name
        raw = (environ.get(var) or "").strip()
        settings.append({"setting": var, "state": f"set: {raw}" if raw else f"default {default:g}", "meaning": meaning})
    for var, meaning in NAMED_SETTINGS:
        settings.append({"setting": var, "state": "set" if (environ.get(var) or "").strip() else "unset",
                         "meaning": meaning})
    not_judged = [] if wcfg.slot_models else [f"open rows no keeper slot can take: set {ENV_PREFIX}{SLOT_MODELS_VAR}"]
    return {"settings": settings, "keys": list(KEYS), "metrics_not_yet_gathered": list(METRICS_NOT_YET_GATHERED),
            "health_checks_not_judged": not_judged, "doc": SESSIONVIEW_DOC}
