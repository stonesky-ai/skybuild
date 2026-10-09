#!/usr/bin/env python3
"""Start Claude workers on named accounts, and pick the account with the most room.

Owner's-machine tool for SkyKeep's parallel build. Each Claude account has its
own config directory (what `CLAUDE_CONFIG_DIR` names: its credentials, its
`.claude.json`) and its own usage file, the JSON the `poll` verb writes. The
accounts file names them, one account per line, `#` comments allowed and a
path with spaces quoted:

    # name     config directory            usage file
    main       /srv/claude/main            /srv/claude/usage/main.json
    second     /srv/claude/second          /srv/claude/usage/second.json

Verbs:

    status                     every account's five_hour and seven_day percent,
                               reset times, room under the stop line and state
    pick                       the eligible account with the most room per hour
                               left before its seven_day reset (ties: by name)
    run <name|auto> -- <cmd>   exec <cmd> with CLAUDE_CONFIG_DIR set to that
                               account's directory, and nothing else changed
    poll <name>                fetch that account's usage into its usage file

An account is eligible only when its usage is known and under the stop line. A
missing, stale or non-ok usage file, or one without a `seven_day` percentage or
a future `seven_day` reset time, is unknown, never 0 %, and is never picked.
`pick` and `run auto` exit 1 and start nothing when no account is eligible.

`poll` behaves like the owner's existing single-account `claude-usage-poll`
script: the same request headers, the same backoff (persisted in the usage
file, so a timer firing every five minutes still respects it), the same file
shape and mode 0600, and it never refreshes or rewrites the credentials file
(Claude Code owns it). It also refuses a redirect, which would carry the
bearer token to wherever the redirect points, and it never writes the token,
or any header value, to its output, its usage file or an error message.

Every path, endpoint and threshold comes from a flag or its environment
variable (ADR-0002); there are no defaults, and a verb missing one it needs
refuses to start. `--help` on each verb lists its flags with their variables.
"""

from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import json
import math
import os
import re
import shlex
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

#: A name is a systemd instance (`claude-usage-poll@<name>`) and a word in a
#: command line, so it is kept to characters that need no quoting in either.
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
#: `run auto` picks; no account may be called that.
AUTO = "auto"
#: What Claude Code reads to find its config directory.
CONFIG_DIR_VAR = "CLAUDE_CONFIG_DIR"
#: Claude Code's own file name for the OAuth credentials in a config directory.
CREDENTIALS = ".credentials.json"
#: RFC 6750 b64token: anything else is not a bearer token, and a value with a
#: newline in it would put the token into http.client's error message.
BEARER = re.compile(r"^[A-Za-z0-9._~+/=-]+$")

# The poller's behaviour, the same numbers as the reference script's.
TIMEOUT_SECONDS = 20
BACKOFF_START = 600      # 10 min after the first failure
BACKOFF_429 = 900        # a 429 from this endpoint lasts minutes, not seconds
BACKOFF_CAP = 3600       # never wait longer than an hour
MIN_INTERVAL = 240       # never poll more often than every 4 min
ERROR_TEXT_CAP = 300     # how much of an error body the usage file keeps
ERROR_READ_CAP = 1 << 20  # how much of an error body is read (scrubbed, then capped)
BETA = "oauth-2025-04-20"
USER_AGENT = "claude-cli/2.1.247 (external, cli)"

BUCKETS = ("five_hour", "seven_day", "seven_day_opus")
PCT_KEYS = ("utilization", "percent_used", "used_percent",
            "utilization_percent", "percentage", "percent")
RESET_KEYS = ("resets_at", "reset_at", "resetsAt", "next_reset")

#: What stands where a secret was.
REDACTED = "[redacted]"

EXIT_NONE_ELIGIBLE = 1
EXIT_REFUSED = 2
EXIT_NOT_STARTED = 127


class Refused(Exception):
    """A configuration or request this tool will not act on (exit 2)."""


class CredentialsError(Exception):
    """The credentials file gave no usable token. Its text never names the token."""


# --------------------------------------------------------------------------
# The accounts file
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Account:
    name: str
    config_dir: Path
    usage_file: Path


def read_accounts(path: Path) -> list[Account]:
    """Every account in the file, or `Refused`: a malformed file is not half-read."""
    try:
        text = path.read_text()
    except OSError as exc:
        raise Refused(f"cannot read the accounts file {path}: {exc.strerror or exc}") from None
    accounts: list[Account] = []
    seen: set[str] = set()
    for number, line in enumerate(text.splitlines(), 1):
        try:
            fields = shlex.split(line, comments=True)
        except ValueError as exc:
            raise Refused(f"{path}:{number}: {exc}") from None
        if not fields:
            continue
        if len(fields) != 3:
            raise Refused(f"{path}:{number}: want `<name> <config-dir> <usage-file>`, got {len(fields)} field(s)")
        name, config_dir, usage_file = fields
        if not NAME.match(name):
            raise Refused(f"{path}:{number}: {name!r} is not an account name ({NAME.pattern})")
        if name == AUTO:
            raise Refused(f"{path}:{number}: {AUTO!r} is reserved for `run {AUTO}`")
        if name in seen:
            raise Refused(f"{path}:{number}: account {name!r} is named twice")
        seen.add(name)
        paths = []
        for what, raw in (("config directory", config_dir), ("usage file", usage_file)):
            p = Path(raw).expanduser()
            if not p.is_absolute():
                raise Refused(f"{path}:{number}: the {what} {raw!r} is not an absolute path")
            paths.append(p)
        accounts.append(Account(name, paths[0], paths[1]))
    if not accounts:
        raise Refused(f"{path} names no account")
    return accounts


def find(accounts: list[Account], name: str) -> Account:
    for account in accounts:
        if account.name == name:
            return account
    raise Refused(f"no account named {name!r} in the accounts file")


# --------------------------------------------------------------------------
# Reading a usage file
# --------------------------------------------------------------------------

OK = "ok"
UNKNOWN = "unknown"
AT_STOP = "at stop line"


@dataclass(frozen=True)
class Reading:
    account: Account
    state: str
    reason: str = ""
    five_hour_pct: float | None = None
    five_hour_resets: object = None
    seven_day_pct: float | None = None
    seven_day_resets: object = None
    room: float | None = None
    room_per_hour: float | None = None

    @property
    def eligible(self) -> bool:
        return self.state == OK


def _pct(bucket: object) -> float | None:
    pct = bucket.get("pct") if isinstance(bucket, dict) else None
    if isinstance(pct, bool) or not isinstance(pct, (int, float)):
        return None
    if not math.isfinite(pct) or pct < 0:
        return None
    return float(pct)


def reset_epoch(value: object) -> float | None:
    """A `resets_at` as epoch seconds, or None when it cannot be read unambiguously."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            when = dt.datetime.fromisoformat(value)
        except ValueError:
            return None
        # A time with no zone is ambiguous by hours, which is the whole answer.
        return when.timestamp() if when.tzinfo is not None else None
    return None


def assess(account: Account, *, now: float, stop_pct: float, max_age_minutes: float) -> Reading:
    """What the account's usage file says, read the way `codex_loop.py` reads it.

    A missing, stale or non-ok file is unknown, never 0 % (`CLAUDE.md`).
    """
    path = account.usage_file
    try:
        data = json.loads(path.read_text())
    except OSError:
        return Reading(account, UNKNOWN, f"{path} is missing or unreadable")
    except ValueError:
        return Reading(account, UNKNOWN, f"{path} is not JSON")
    if not isinstance(data, dict):
        return Reading(account, UNKNOWN, f"{path} is not a usage record")
    buckets = data.get("buckets") if isinstance(data.get("buckets"), dict) else {}
    five, seven = buckets.get("five_hour"), buckets.get("seven_day")
    shown = {
        "five_hour_pct": _pct(five),
        "five_hour_resets": five.get("resets_at") if isinstance(five, dict) else None,
        "seven_day_pct": _pct(seven),
        "seven_day_resets": seven.get("resets_at") if isinstance(seven, dict) else None,
    }

    def unknown(why: str) -> Reading:
        return Reading(account, UNKNOWN, why, **shown)

    if data.get("status") != "ok":
        return unknown(f"status {data.get('status')!r}, not 'ok'")
    if data.get("stale") is not False:
        return unknown("the file says it is stale")
    fetched = data.get("fetched_at")
    if isinstance(fetched, bool) or not isinstance(fetched, (int, float)) or not math.isfinite(fetched):
        return unknown("no fetched_at")
    age = now - fetched
    if age > max_age_minutes * 60:
        return unknown(f"fetched {age / 60:.0f} min ago (limit {max_age_minutes:g} min)")
    pct = shown["seven_day_pct"]
    if pct is None:
        return unknown("no seven_day percentage")
    room = stop_pct - pct
    if pct >= stop_pct:
        return Reading(account, AT_STOP, f"seven_day {pct:g} % (stop line {stop_pct:g} %)", room=room, **shown)
    reset = reset_epoch(shown["seven_day_resets"])
    if reset is None:
        return unknown("no seven_day reset time")
    if reset <= now:
        return unknown("the seven_day reset time has passed; the file predates the new window")
    return Reading(account, OK, room=room, room_per_hour=room / ((reset - now) / 3600), **shown)


def pick(readings: list[Reading]) -> Reading | None:
    """The eligible reading with the most room per hour left, ties broken by name."""
    eligible = [r for r in readings if r.eligible]
    if not eligible:
        return None
    return min(eligible, key=lambda r: (-r.room_per_hour, r.account.name))


def _cell(value: object, cap: int | None = 40) -> str:
    """A table cell: the file's text is not trusted to be printable."""
    text = "-" if value is None else str(value)
    text = "".join(ch if ch.isprintable() else "?" for ch in text)
    return text if cap is None or len(text) <= cap else text[: cap - 1] + "~"


def _num(value: float | None, fmt: str) -> str:
    return "-" if value is None else format(value, fmt)


def render(readings: list[Reading], stop_pct: float) -> str:
    head = ("account", "5h %", "5h resets", "7d %", "7d resets", "room", "room/h", "state")
    rows = [head]
    for r in readings:
        state = r.state if r.state == OK else f"{r.state}: {r.reason}"
        rows.append((_cell(r.account.name), _num(r.five_hour_pct, ".1f"), _cell(r.five_hour_resets),
                     _num(r.seven_day_pct, ".1f"), _cell(r.seven_day_resets), _num(r.room, ".1f"),
                     _num(r.room_per_hour, ".3f"), _cell(state, None)))
    widths = [max(len(row[i]) for row in rows) for i in range(len(head) - 1)]
    lines = [f"stop line {stop_pct:g} % of seven_day"]
    for row in rows:
        lines.append("  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=False)) + "  " + row[-1])
    return "\n".join(line.rstrip() for line in lines)


# --------------------------------------------------------------------------
# Polling one account's usage
# --------------------------------------------------------------------------


def check_url(url: str) -> str:
    """The usage endpoint, or `Refused`. The request carries a bearer token.

    So it goes over TLS, or in clear only to this machine's loopback (the
    tests' stub), and a URL never carries credentials of its own.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.username is not None or parts.password is not None:
        raise Refused("the usage URL carries credentials; a URL never does")
    host = parts.hostname or ""
    if not host:
        raise Refused(f"the usage URL {url!r} names no host")
    if parts.scheme == "https":
        return url
    if parts.scheme == "http":
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
        if loopback:
            return url
        raise Refused("the usage URL is plain http to another machine; the bearer token goes over https only")
    raise Refused(f"the usage URL's scheme {parts.scheme!r} is not https")


def scrub(value: object, secret: str) -> object:
    """`value` with every occurrence of `secret` replaced, however deep it sits."""
    if isinstance(value, str):
        return value.replace(secret, REDACTED)
    if isinstance(value, dict):
        return {scrub(k, secret): scrub(v, secret) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v, secret) for v in value]
    return value


def access_token(config_dir: Path) -> str:
    """The OAuth access token Claude Code keeps in `config_dir`. Read, never written."""
    path = config_dir / CREDENTIALS
    try:
        text = path.read_text()
    except OSError:
        raise CredentialsError(f"{CREDENTIALS} is missing or unreadable") from None
    except ValueError:
        raise CredentialsError(f"{CREDENTIALS} is not text") from None
    try:
        data = json.loads(text)
    except ValueError:
        raise CredentialsError(f"{CREDENTIALS} is not JSON") from None
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    token = oauth.get("accessToken") if isinstance(oauth, dict) else None
    if not isinstance(token, str) or not token:
        raise CredentialsError(f"{CREDENTIALS} holds no OAuth access token")
    if not BEARER.match(token):
        raise CredentialsError(f"{CREDENTIALS} holds an access token that is not a bearer token")
    return token


def find_bucket(obj: object, name: str) -> dict | None:
    """A named bucket anywhere in the response (the nesting is not a published contract)."""
    if isinstance(obj, dict):
        if name in obj and isinstance(obj[name], dict):
            return obj[name]
        children = obj.values()
    elif isinstance(obj, list):
        children = obj
    else:
        return None
    for child in children:
        hit = find_bucket(child, name)
        if hit is not None:
            return hit
    return None


def as_pct(value: object) -> float | None:
    """0-100. A float at or below 1.0 is a fraction; an int is already a percentage."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return float(value)
    if isinstance(value, float):
        return value * 100.0 if value <= 1.0 else value
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_usage(payload: object) -> dict:
    out = {}
    for name in BUCKETS:
        bucket = find_bucket(payload, name)
        if not bucket:
            continue
        pct = None
        for key in PCT_KEYS:
            if key in bucket:
                pct = as_pct(bucket[key])
                if pct is not None:
                    break
        reset = None
        for key in RESET_KEYS:
            if key in bucket:
                reset = bucket[key]
                break
        if pct is not None and math.isfinite(pct):
            out[name] = {"pct": round(pct, 1), "resets_at": reset}
    return out


def load_usage(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_usage(path: Path, data: dict) -> None:
    """Atomically, mode 0600: a reader never sees a half-written file."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _seconds(value: object) -> float:
    """A number the usage file stored, or 0: a hand-edited file cannot crash the poller."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return 0.0
    return float(value)


def _backoff(cache: dict, first: int) -> float:
    prev = _seconds(cache.get("backoff"))
    return min(prev * 2, BACKOFF_CAP) if prev > 0 else first


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would resend the Authorization header to wherever it points."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # None makes urllib raise the 3xx as an HTTPError, which poll() records.
        return None


def _open(request: urllib.request.Request):
    opener = urllib.request.build_opener(
        _NoRedirect, urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    return opener.open(request, timeout=TIMEOUT_SECONDS)


def poll(account: Account, *, url: str, now: float, force: bool = False) -> str:
    """Fetch the account's usage into its usage file. Returns the status it wrote.

    Raises OSError only when the usage file cannot be written; every failure to
    fetch is a normal condition recorded in the file, as the reference does.
    """
    cache = load_usage(account.usage_file)
    if not force:
        after = _seconds(cache.get("next_poll_after"))
        if now < after:
            return f"backing off until {dt.datetime.fromtimestamp(after, tz=dt.UTC).isoformat()}"
        if now - _seconds(cache.get("fetched_at")) < MIN_INTERVAL:
            return "fresh: fetched under the minimum interval ago"

    try:
        token = access_token(account.config_dir)
    except CredentialsError as exc:
        cache.update(status="noauth", error=str(exc), checked_at=now, stale=True)
        write_usage(account.usage_file, cache)
        return "noauth"

    request = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": BETA,
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })
    try:
        with _open(request) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read(ERROR_READ_CAP).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - an unreadable error body is only a missing detail
            body = ""
        finally:
            exc.close()
        if exc.code in (401, 403):
            # Claude Code rotates this token on its own; nothing to do but wait.
            status, wait = "auth", BACKOFF_START
        elif exc.code == 429:
            status, wait = "ratelimited", _backoff(cache, BACKOFF_429)
        else:
            status, wait = f"http{exc.code}", _backoff(cache, BACKOFF_START)
        cache.update(status=status, error=scrub(body, token)[:ERROR_TEXT_CAP], checked_at=now,
                     stale=True, backoff=wait, next_poll_after=now + wait)
        write_usage(account.usage_file, cache)
        return status
    except Exception as exc:  # noqa: BLE001 - any other failure is a network error, recorded
        wait = _backoff(cache, BACKOFF_START)
        cache.update(status="neterr", error=scrub(str(exc), token)[:ERROR_TEXT_CAP], checked_at=now,
                     stale=True, backoff=wait, next_poll_after=now + wait)
        write_usage(account.usage_file, cache)
        return "neterr"

    buckets = parse_usage(payload)
    result = {
        "status": "ok" if buckets else "noparse",
        "stale": False,
        "fetched_at": now,
        "checked_at": now,
        "backoff": 0,
        "next_poll_after": 0,
        "buckets": buckets,
    }
    if not buckets:
        # An unfamiliar response is only fixable if it was kept; mode 0600.
        result["raw"] = scrub(payload, token)
    write_usage(account.usage_file, result)
    return result["status"]


# --------------------------------------------------------------------------
# The command line
# --------------------------------------------------------------------------

#: (flag, environment variable, type, help). None has a default: from the
#: flag, else the variable, else a verb that needs it refuses to start.
SETTINGS: tuple[tuple[str, str, type, str], ...] = (
    ("--accounts", "SKYKEEP_CLAUDE_ACCOUNTS", str,
     "the accounts file, one `<name> <config-dir> <usage-file>` line per account"),
    ("--weekly-stop-pct", "SKYKEEP_CLAUDE_WEEKLY_STOP_PCT", float,
     "an account whose seven_day percent is at or above this is never picked"),
    ("--usage-max-age-minutes", "SKYKEEP_CLAUDE_USAGE_MAX_AGE_MINUTES", float,
     "a usage file fetched longer ago than this is stale"),
    ("--usage-url", "SKYKEEP_CLAUDE_USAGE_URL", str, "the usage endpoint `poll` asks"),
)
READING = ("--accounts", "--weekly-stop-pct", "--usage-max-age-minutes")
#: The settings each verb needs. `run <name>` reads no usage; `run auto` does.
NEEDS: dict[str, tuple[str, ...]] = {
    "status": READING,
    "pick": READING,
    "run": ("--accounts",),
    "run auto": READING,
    "poll": ("--accounts", "--usage-url"),
}


def _dest(flag: str) -> str:
    return flag[2:].replace("-", "_")


def parse(argv: list[str], environ: Mapping[str, str] | None = None) -> argparse.Namespace:
    env = os.environ if environ is None else environ
    command: list[str] = []
    if argv and argv[0] == "run" and "--" in argv:
        # Everything after the first `--` is the command, flags and all.
        cut = argv.index("--")
        argv, command = argv[:cut], argv[cut + 1:]
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    common = argparse.ArgumentParser(add_help=False)
    for flag, var, kind, text in SETTINGS:
        common.add_argument(flag, type=kind, default=env.get(var) or None, help=f"{text} (env {var})")
    verbs = p.add_subparsers(dest="verb", required=True, metavar="{status,pick,run,poll}")
    verbs.add_parser("status", parents=[common], help="every account's usage and state")
    verbs.add_parser("pick", parents=[common], help="print the eligible account with the most room")
    run = verbs.add_parser("run", parents=[common], usage="%(prog)s [flags] <name|auto> -- <command...>",
                           help="exec a command on an account")
    run.add_argument("account", help=f"an account's name, or {AUTO} for the one `pick` prints")
    poll_p = verbs.add_parser("poll", parents=[common], help="fetch an account's usage into its usage file")
    poll_p.add_argument("account", help="an account's name")
    poll_p.add_argument("--force", action="store_true", help="ignore the backoff and the minimum interval")
    a = p.parse_args(argv)
    a.command = command
    if a.verb == "run" and not command:
        p.error("run needs `-- <command...>`: the command to start on the account")
    need = NEEDS["run auto" if a.verb == "run" and a.account == AUTO else a.verb]
    missing = [f"{flag} (or {var})" for flag, var, _, _ in SETTINGS
               if flag in need and getattr(a, _dest(flag)) is None]
    if missing:
        p.error("not configured, and nothing here has a default (ADR-0002): " + ", ".join(missing))
    if "--weekly-stop-pct" in need and not (math.isfinite(a.weekly_stop_pct) and 0 < a.weekly_stop_pct <= 100):
        p.error(f"--weekly-stop-pct {a.weekly_stop_pct} is not a percentage above 0 and at most 100")
    if "--usage-max-age-minutes" in need and not (
            math.isfinite(a.usage_max_age_minutes) and a.usage_max_age_minutes > 0):
        p.error(f"--usage-max-age-minutes {a.usage_max_age_minutes} is not a positive number")
    if "--usage-url" in need:
        try:
            check_url(a.usage_url)
        except Refused as exc:
            p.error(str(exc))
    return a


def main(argv: list[str], environ: Mapping[str, str] | None = None, *,
         clock: Callable[[], float] = time.time,
         execvpe: Callable[[str, list[str], dict[str, str]], object] = os.execvpe) -> int:
    env = os.environ if environ is None else environ
    a = parse(argv, env)
    try:
        accounts = read_accounts(Path(a.accounts).expanduser())
        if a.verb == "poll":
            account = find(accounts, a.account)
            try:
                status = poll(account, url=a.usage_url, now=clock(), force=a.force)
            except OSError as exc:
                print(f"poll {account.name}: cannot write {account.usage_file}: {exc.strerror or type(exc).__name__}",
                      file=sys.stderr)
                return 1
            print(f"poll {account.name}: {status}")
            return 0
        if a.verb == "run" and a.account != AUTO:
            chosen = find(accounts, a.account)
        else:
            now = clock()
            readings = [assess(acc, now=now, stop_pct=a.weekly_stop_pct, max_age_minutes=a.usage_max_age_minutes)
                        for acc in accounts]
            if a.verb == "status":
                print(render(readings, a.weekly_stop_pct))
                return 0
            best = pick(readings)
            if best is None:
                print("no account is eligible: each is unknown or at the stop line (`status` says which)",
                      file=sys.stderr)
                return EXIT_NONE_ELIGIBLE
            if a.verb == "pick":
                print(best.account.name)
                return 0
            chosen = best.account
        if not chosen.config_dir.is_dir():
            raise Refused(f"account {chosen.name!r}: its config directory {chosen.config_dir} does not exist")
    except Refused as exc:
        print(f"claude_accounts: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    child = {**env, CONFIG_DIR_VAR: str(chosen.config_dir)}
    try:
        execvpe(a.command[0], a.command, child)
    except OSError as exc:
        print(f"claude_accounts: cannot start {a.command[0]!r}: {exc.strerror or type(exc).__name__}",
              file=sys.stderr)
        return EXIT_NOT_STARTED
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
