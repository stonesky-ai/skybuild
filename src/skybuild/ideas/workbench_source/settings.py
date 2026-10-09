"""Settings: the terminal view's `Config` and the page's `WebConfig`, every SESSIONVIEW_* and
SESSIONVIEW_WEB_* name, the dials, the port check and the page's clock. Configuration comes from the
environment only (ADR-0002).
"""
from __future__ import annotations

import argparse
import math
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import hub as sv

if TYPE_CHECKING:      # names used in annotations only (strings at runtime)
    from .sources.summaries import SummaryConfig


# ---------------------------------------------------------------- configuration

ENV_PREFIX = "SESSIONVIEW_"


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(ENV_PREFIX + name, default)


@dataclass
class Config:
    repo: Path
    git_dir: Path
    todo: Path
    projects_dir: Path
    watchdog_pattern: str
    watchdog_log: Path | None
    interval: float
    stale_minutes: float
    show_dirty: bool
    color: bool
    once: bool
    codex_state: Path | None = None
    codex_home: Path | None = None
    codex_loop_pattern: str = "skykeep-codex-loop"
    remote: str = "origin"
    # State directories of the unattended streams (each loop's `--state-dir`);
    # their `.agent-state` files say how far a seam has got and why it waits.
    stream_state: list[Path] = field(default_factory=list)
    # An unready seam tip older than this is STUCK; None checks no idleness.
    seam_idle_minutes: float | None = None
    # `--serve` listens on the loopback interface at `port`. The port has no
    # default: a missing one refuses (ADR-0002).
    serve: bool = False
    port: int | None = None
    # Grok's home: its `sessions/` directories and `logs/unified.jsonl` are the
    # Grok panel's only signals.
    grok_home: Path | None = None
    # Trunk branch passed through to scripts/claims.py. None defers to
    # SKYKEEP_GIT_TRUNK, and neither set refuses the split rather than guessing.
    trunk: str | None = None
    # True only when --stream-state / SESSIONVIEW_STREAM_STATE named the
    # `stream_state` directories. The seams fall back to the Codex state
    # directory; the loops' current steps never do: with none named they
    # refuse, naming the setting (ADR-0002).
    stream_state_named: bool = False
    # How many merges may sit above the last ledger commit, as given (unparsed:
    # `scripts/seam_merge.py` reads it, as it reads its own). None defers to
    # that script's setting; nothing set shows as no limit, never as a number.
    max_unledgered_merges: str | None = None


def legacy_resolve_config(argv: list[str] | None = None) -> Config:
    p = argparse.ArgumentParser(
        prog="sessionview",
        description="Live view of a Claude Code session working off a to-do file.",
    )
    p.add_argument("--repo", default=_env("REPO"), help="repo root (default: git rev-parse from cwd)")
    p.add_argument("--todo", default=_env("TODO"), help="to-do file (default: <repo>/MasterToDo.md)")
    p.add_argument("--projects-dir", default=_env("PROJECTS_DIR"),
                   help="Claude Code session directory (default: derived from the repo path)")
    p.add_argument("--watchdog-pattern", default=_env("WATCHDOG_PATTERN", "skykeep-watchdog"),
                   help="substring identifying the watchdog process (default: skykeep-watchdog)")
    p.add_argument("--watchdog-log", default=_env("WATCHDOG_LOG"),
                   help="watchdog log file (default: ~/.claude/<pattern>.log if present)")
    p.add_argument("--interval", type=float, default=float(_env("INTERVAL", "15") or 15),
                   help="refresh seconds (default: 15)")
    p.add_argument("--stale-minutes", type=float, default=float(_env("STALE_MINUTES", "15") or 15),
                   help="flag an agent quiet for this long (default: 15)")
    p.add_argument("--codex-state", default=_env("CODEX_STATE", "~/.local/state/skykeep-gpt"),
                   help="the Codex loop's state directory; a running loop's own --state-dir "
                        "wins (default: ~/.local/state/skykeep-gpt)")
    p.add_argument("--codex-home", default=_env("CODEX_HOME", os.environ.get("CODEX_HOME", "~/.codex")),
                   help="Codex's home, for the step's session rollout (default: $CODEX_HOME or ~/.codex)")
    p.add_argument("--codex-loop-pattern", default=_env("CODEX_LOOP_PATTERN", "skykeep-codex-loop"),
                   help="file name of the Codex loop script (default: skykeep-codex-loop)")
    p.add_argument("--no-codex", action="store_true", help="skip the Codex loop panel")
    p.add_argument("--remote", default=_env("REMOTE", "origin"),
                   help="remote whose seam/* branches are listed (default: origin)")
    p.add_argument("--stream-state", default=_env("STREAM_STATE"),
                   help="state directories of the unattended streams, separated by "
                        f"'{os.pathsep}'; each loop log names its stream and checkout, whose "
                        ".agent-state files mark the seams (default: the --codex-state directory)")
    p.add_argument("--seam-idle-minutes", type=float, default=_env("SEAM_IDLE_MINUTES"),
                   help="flag an unready seam tip older than this as STUCK "
                        "(default: unset, no idle check)")
    p.add_argument("--no-dirty", action="store_true", help="skip per-worktree dirty counts")
    p.add_argument("--no-color", action="store_true", help="plain output")
    p.add_argument("--once", action="store_true", help="print one frame and exit")
    p.add_argument("--serve", action="store_true",
                   help="serve the live HTML page instead of the terminal view")
    p.add_argument("--port", type=int, default=None,
                   help="TCP port for --serve (required; there is no default port)")
    p.add_argument("--grok-home", default=_env("GROK_HOME"),
                   help="Grok's home: live sessions are its sessions/<url-encoded cwd>/<session id>/ "
                        "directories, matched against logs/unified.jsonl "
                        "(default: $GROK_HOME or ~/.grok)")
    p.add_argument("--trunk", default=_env("TRUNK", os.environ.get("SKYKEEP_GIT_TRUNK")),
                   help="trunk branch for the MasterToDo claim split, the same role "
                        "as claims.py --trunk (default: SKYKEEP_GIT_TRUNK; neither set "
                        "refuses the split)")
    p.add_argument("--max-unledgered-merges", default=_env("MAX_UNLEDGERED_MERGES"), metavar="N",
                   help="how many merges may sit above the last ledger commit, the same role "
                        "as seam_merge.py --max-unledgered-merges (default: that script's own "
                        "setting; nothing set shows as no limit)")
    a = p.parse_args(argv)
    if a.serve and a.once:
        p.error("pass only one of --once and --serve")
    if a.serve and a.port is None:
        p.error("--serve requires --port N (ADR-0002: there is no default port)")
    if a.port is not None and not 1 <= a.port <= 65535:
        p.error("--port must be an integer from 1 to 65535")

    git_dir = _git_common_dir()
    # Always anchor on the MAIN checkout: run from inside an agent worktree and
    # `--show-toplevel` would name that worktree, which then derives the wrong
    # session directory and the wrong to-do file.
    repo = (Path(a.repo).expanduser().resolve() if a.repo
            else (git_dir.parent if git_dir.name == ".git" else _git_root()))
    todo = Path(a.todo).expanduser().resolve() if a.todo else repo / "MasterToDo.md"
    projects = (Path(a.projects_dir).expanduser().resolve() if a.projects_dir
                else _derive_projects_dir(repo))

    log: Path | None = None
    if a.watchdog_log:
        log = Path(a.watchdog_log).expanduser()
    else:
        guess = Path.home() / ".claude" / f"{a.watchdog_pattern}.log"
        if guess.exists():
            log = guess

    color = (not a.no_color) and sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    codex_state = None if a.no_codex else Path(a.codex_state).expanduser()
    if a.stream_state is not None:
        stream_state = [Path(d).expanduser() for d in a.stream_state.split(os.pathsep) if d.strip()]
    else:
        stream_state = [codex_state] if codex_state else []
    if a.seam_idle_minutes is not None and a.seam_idle_minutes <= 0:
        p.error("--seam-idle-minutes must be above zero; leave it unset to check no idleness")
    grok_home = (Path(a.grok_home).expanduser() if a.grok_home
                 else Path(os.environ["GROK_HOME"]).expanduser() if os.environ.get("GROK_HOME")
                 else Path.home() / ".grok")
    trunk = (a.trunk or "").strip() or None
    return Config(repo, git_dir, todo, projects, a.watchdog_pattern, log,
                  max(1.0, a.interval), a.stale_minutes, not a.no_dirty, color, a.once,
                  codex_state, Path(a.codex_home).expanduser(), a.codex_loop_pattern, a.remote,
                  stream_state, a.seam_idle_minutes, a.serve, a.port, grok_home, trunk,
                  stream_state_named=a.stream_state is not None and bool(stream_state),
                  max_unledgered_merges=a.max_unledgered_merges)


def _git_common_dir() -> Path:
    """The main checkout's .git — identical from the repo and from any worktree."""
    out = sv._run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=Path.cwd())
    if out.strip():
        return Path(out.strip()).resolve()
    return (_git_root() / ".git")


def _git_root() -> Path:
    out = sv._run(["git", "rev-parse", "--show-toplevel"], cwd=Path.cwd())
    return Path(out.strip()).resolve() if out else Path.cwd()


def _derive_projects_dir(repo: Path) -> Path:
    """Claude Code slugifies the project path: every non-alphanumeric run becomes '-'."""
    base = Path.home() / ".claude" / "projects"
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(repo))
    direct = base / slug
    if direct.is_dir():
        return direct
    # Fall back to any project directory whose name ends with the repo's basename.
    if base.is_dir():
        tail = re.sub(r"[^A-Za-z0-9]", "-", repo.name)
        cands = [d for d in base.iterdir() if d.is_dir() and d.name.endswith(tail)]
        if cands:
            return max(cands, key=lambda d: d.stat().st_mtime)
    return direct


def page_time(stamp: float, fmt: str) -> str:
    """A date-time as the page shows it: in the zone the integrator's setting names, CST or CDT beside it."""
    return sv.integrator_run.zoned(stamp, fmt)


class Refused(Exception):
    """A configuration this server will not start with. Nothing was bound."""


# ---------------------------------------------------------------- configuration

def lane_port_ranges() -> list[tuple[int, int, str]]:
    """Every port range a lane stack may publish on, read from the lane plan.

    `scripts/gate_run.py` owns the lane-number window, and `gate_lane.sh`
    publishes lane N on `88<N - first + 1>x`. An unreadable plan refuses: a
    guess could hand this server a port a lane is about to need.
    """
    try:
        gate_run = sv._load("gate_run", "gate_run.py")
        first, last = int(gate_run.LANE_FIRST), int(gate_run.LANE_LAST)
    except Exception as exc:  # noqa: BLE001 - any failure is "the plan is unreadable"
        raise Refused(f"cannot read the lane port plan from scripts/gate_run.py: {exc}") from None
    ranges = [(sv.TESTLONG_PORTS[0], sv.TESTLONG_PORTS[1], "the testlong stack")]
    for lane in range(first, last + 1):
        base = int(f"88{lane - first + 1}0")
        ranges.append((base, base + 9, f"gate lane {lane}"))
    return ranges


def check_port(raw: str | None) -> int:
    """The configured port, or Refused. Unset defaults to DEFAULT_PORT (dev box only)."""
    if raw is None or not raw.strip():
        port = sv.DEFAULT_PORT
    else:
        try:
            port = int(raw.strip())
        except ValueError:
            raise Refused(f"{sv.PORT_VAR}={raw!r} is not a port number") from None
        if not 1024 <= port <= 65535:
            raise Refused(f"{sv.PORT_VAR}={port} is outside 1024-65535")
    for low, high, owner in lane_port_ranges():
        if low <= port <= high:
            raise Refused(f"{sv.PORT_VAR}={port} belongs to {owner} ({low}-{high}); "
                          "pick a port clear of the lane stacks")
    return port


#: The operational dials: (name after the prefix, default, meaning). Each is
#: an environment variable; a value that does not parse as a positive number
#: refuses to start rather than falling back.
DIALS: dict[str, tuple[float, str]] = {
    "READY_WAIT_MINUTES": (120.0, ("a READY seam unlanded longer than this is a landing barrier "
                                   "(maude-factory: land when the oldest has waited two hours)")),
    "DISK_LINE_PCT": (80.0, ("the pool is a barrier past this percent full "
                             "(maude-factory § Disk: keep the pool under 80 %)")),
    "DISK_SAMPLE_SECONDS": (60.0, "how often the disk is sampled for the run-out prediction"),
    "RUNOUT_WINDOW_MINUTES": (180.0, "the prediction's rate is fitted over this much history"),
    "RUNOUT_MIN_SPAN_MINUTES": (10.0, "no prediction until the samples span this long"),
    "RUNOUT_WARN_HOURS": (48.0, "a disk predicted to cross its line sooner than this is a barrier"),
    "JOURNAL_MINUTES": (60.0, "how far back the loops' journals are read for memory and usage waits"),
    "SLOW_REFRESH_SECONDS": (60.0, "the loop units and the git history are reread at most this often"),
    "CLEANUP_MIN_AGE_HOURS": (6.0, ("a worktree touched more recently than this is never offered "
                                    "(scripts/cleanup_run.sh's --age-hours default)")),
    "PLAN_TTL_SECONDS": (600.0, "a cleanup plan not run within this long must be previewed again"),
    "OVERSEER_STALE_MINUTES": (15.0, ("the overseer status file older than this reads stale, "
                                      "never healthy (it is written every 5 minutes)")),
    "SUMMARY_TIMEOUT_SECONDS": (60.0, ("a summary the endpoint has not answered within this long is "
                                       "dropped, and its box says so")),
    "SUMMARY_REUSE_SECONDS": (300.0, ("a summary younger than this is shown again rather than asking "
                                      "the shared endpoint anew")),
    "SUMMARY_MAX_TOKENS": (400.0, ("the most tokens one summary may run to (the endpoint's num_predict); "
                                   "a summary cut off at it is never shown")),
    "BATCH_HISTORY_SHOWN": (10.0, "how many past batches the Previous Batch Stats table lists, newest first"),
    "TODO_PROBE_SECONDS": (60.0, ("the todo service's /health and /queue and the boxes' reach records are "
                                  "read again, beside the page, at most this often")),
    "TODO_PROBE_TIMEOUT_SECONDS": (5.0, ("a todo service /health or /queue read not answered within this "
                                         "long counts as no answer")),
    "TODO_REACH_MAX_AGE_SECONDS": (900.0, ("a box's reach record older than this is shown as STALE, never "
                                           "as ok (scripts/todo_service/reach_probe.sh writes it)")),
    "TODO_UNTAKEN_MINUTES": (30.0, ("open todo items with no box holding a live claim for this long are "
                                    "a warning; a claim whose heartbeat is this old holds nothing")),
    "TODO_SYNC_STALE_MINUTES": (15.0, ("a seam scan (the todo service's /integration scanned_at) older than "
                                       "this is a health flag: the integration board is stale")),
    "STATE_WAIT_SECONDS": (45.0, ("how long a page request waits for the state when none is built yet, the "
                                  "last one is older than STATE_IDLE_REFRESHES refreshes, or a button was "
                                  "just pressed; past it the last state is served as it is")),
    "STATE_IDLE_REFRESHES": (4.0, ("refresh intervals with no page asking after which the server stops "
                                   "rebuilding the state; the next request starts it again")),
}
#: The models the keepers' slots run, a comma list (`SESSIONVIEW_WEB_SLOT_MODELS=sonnet,opus,bash`);
#: with it set, open rows no slot can take are a health flag. Unset: that flag is not judged.
SLOT_MODELS_VAR = "SLOT_MODELS"
#: The loop units read when SESSIONVIEW_WEB_LOOP_UNITS names none: every user
#: unit matching this pattern (the unattended loops' naming, docs/dev/codex.md).
DEFAULT_LOOP_UNIT_GLOB = "skykeep-*-loop.service"
#: Dials that are percentages must also be at most 100.
PERCENT_DIALS = {"DISK_LINE_PCT"}


def _dial(env: Mapping[str, str], name: str) -> float:
    default, _meaning = DIALS[name]
    raw = env.get(sv.ENV + name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        raise Refused(f"{sv.ENV}{name}={raw!r} is not a number") from None
    if not value > 0 or math.isnan(value) or value == float("inf"):
        raise Refused(f"{sv.ENV}{name}={raw!r} must be a positive number")
    if name in PERCENT_DIALS and value > 100:
        raise Refused(f"{sv.ENV}{name}={raw!r} is a percentage: at most 100")
    return value


@dataclass
class WebConfig:
    port: int
    sv: Any                       # sessionview's Config: repo, to-do file, streams, trunk
    unresolved: Path
    zpool: str | None = None      # None: the pool holding the repo, when it is ZFS
    ready_wait_minutes: float = DIALS["READY_WAIT_MINUTES"][0]
    disk_line_pct: float = DIALS["DISK_LINE_PCT"][0]
    disk_sample_seconds: float = DIALS["DISK_SAMPLE_SECONDS"][0]
    runout_window_minutes: float = DIALS["RUNOUT_WINDOW_MINUTES"][0]
    runout_min_span_minutes: float = DIALS["RUNOUT_MIN_SPAN_MINUTES"][0]
    runout_warn_hours: float = DIALS["RUNOUT_WARN_HOURS"][0]
    journal_minutes: float = DIALS["JOURNAL_MINUTES"][0]
    slow_refresh_seconds: float = DIALS["SLOW_REFRESH_SECONDS"][0]
    cleanup_min_age_hours: float = DIALS["CLEANUP_MIN_AGE_HOURS"][0]
    plan_ttl_seconds: float = DIALS["PLAN_TTL_SECONDS"][0]
    summary_timeout_seconds: float = DIALS["SUMMARY_TIMEOUT_SECONDS"][0]
    summary_reuse_seconds: float = DIALS["SUMMARY_REUSE_SECONDS"][0]
    summary_max_tokens: float = DIALS["SUMMARY_MAX_TOKENS"][0]
    batch_history_shown: float = DIALS["BATCH_HISTORY_SHOWN"][0]
    todo_probe_seconds: float = DIALS["TODO_PROBE_SECONDS"][0]
    todo_probe_timeout_seconds: float = DIALS["TODO_PROBE_TIMEOUT_SECONDS"][0]
    todo_reach_max_age_seconds: float = DIALS["TODO_REACH_MAX_AGE_SECONDS"][0]
    todo_untaken_minutes: float = DIALS["TODO_UNTAKEN_MINUTES"][0]
    todo_sync_stale_minutes: float = DIALS["TODO_SYNC_STALE_MINUTES"][0]
    state_wait_seconds: float = DIALS["STATE_WAIT_SECONDS"][0]
    state_idle_refreshes: float = DIALS["STATE_IDLE_REFRESHES"][0]
    slot_models: tuple[str, ...] | None = None     # None: SLOT_MODELS unset, the no-slot flag not judged
    summary: SummaryConfig | None = None           # None: no endpoint named, no Summaries box
    loop_units: tuple[str, ...] | None = None      # None: discovered by loop_unit_glob
    loop_unit_glob: str = DEFAULT_LOOP_UNIT_GLOB
    mem_floor_gib: float | None = None             # None: the loops' own --min-mem-gib
    # The Claude usage readings (claude_accounts.py's variables); None: the loops' flags.
    claude_accounts: Path | None = None
    claude_usage: Path | None = None
    weekly_stop_pct: float | None = None
    usage_max_age_minutes: float | None = None
    # The metered endpoint: a box that bills by the hour and must be shut off
    # once nothing needs it. None: none is named, and the page has no banner.
    metered_name: str | None = None
    metered_endpoint: str | None = None
    metered_ack: Path | None = None   # exists: the owner says the box is off, no banner
    metered_howto: str | None = None   # what to press to shut it down, shown in the banner
    reviews_dir: Path | None = None    # the review seat files; None: every seam counts
    gate_conf: Path | None = None
    timing_data_dir: Path | None = None
    overseer_status: Path | None = None
    overseer_stale_minutes: float = DIALS["OVERSEER_STALE_MINUTES"][0]


def resolve_config(environ: Mapping[str, str] | None = None) -> WebConfig:
    """Everything from the environment; Refused on anything missing or wrong."""
    env = os.environ if environ is None else environ
    port = check_port(env.get(sv.PORT_VAR))
    dials = {name.lower(): _dial(env, name) for name in DIALS}
    cfg = legacy_resolve_config(["--no-color"])
    unresolved = env.get(sv.ENV + "UNRESOLVED", "").strip()
    units = tuple(u.strip() for u in env.get(sv.ENV + "LOOP_UNITS", "").split(",") if u.strip())
    accounts_file = env.get("SKYKEEP_CLAUDE_ACCOUNTS", "").strip()
    gate_conf = env.get(sv.ENV + "GATE_CONF", "").strip()
    metered_ack = env.get(sv.ENV + "METERED_ACK_FILE", "").strip()
    metered_howto = " ".join(env.get(sv.ENV + "METERED_HOWTO", "").split())
    reviews_dir = env.get(sv.ENV + "REVIEWS_DIR", "").strip()
    usage_file = env.get(sv.ENV + "CLAUDE_USAGE", "").strip()
    overseer = env.get(sv.ENV + "OVERSEER_STATUS", "").strip()
    timing_dir = env.get("SKYKEEP_TIMING_DATA_DIR", "").strip()
    slot_models = tuple(m.strip() for m in env.get(sv.ENV + SLOT_MODELS_VAR, "").replace(",", " ").split()
                        if m.strip())
    return WebConfig(port=port, sv=cfg, slot_models=slot_models or None,
                     unresolved=Path(unresolved).expanduser() if unresolved else cfg.repo / "Unresolved.md",
                     zpool=env.get(sv.ENV + "ZPOOL", "").strip() or None,
                     loop_units=units or None,
                     loop_unit_glob=env.get(sv.ENV + "LOOP_UNIT_GLOB", "").strip() or DEFAULT_LOOP_UNIT_GLOB,
                     mem_floor_gib=sv._optional_number(env, sv.ENV + "MEM_FLOOR_GIB"),
                     claude_accounts=Path(accounts_file).expanduser() if accounts_file else None,
                     claude_usage=Path(usage_file).expanduser() if usage_file else None,
                     weekly_stop_pct=sv._optional_number(env, "SKYKEEP_CLAUDE_WEEKLY_STOP_PCT", percent=True),
                     usage_max_age_minutes=sv._optional_number(env, "SKYKEEP_CLAUDE_USAGE_MAX_AGE_MINUTES"),
                     summary=sv.resolve_summary(env),
                     metered_name=env.get(sv.ENV + "METERED_NAME", "").strip() or None,
                     metered_endpoint=env.get(sv.ENV + "METERED_ENDPOINT", "").strip() or None,
                     metered_ack=Path(metered_ack).expanduser() if metered_ack else None,
                     metered_howto=metered_howto or None,
                     reviews_dir=Path(reviews_dir).expanduser() if reviews_dir else None,
                     gate_conf=Path(gate_conf).expanduser() if gate_conf else None,
                     timing_data_dir=Path(timing_dir).expanduser() if timing_dir else None,
                     overseer_status=Path(overseer).expanduser() if overseer else None,
                     **dials)
