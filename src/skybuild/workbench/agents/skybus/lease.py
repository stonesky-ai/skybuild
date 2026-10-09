"""Integrator lease: renew, challenge, and the fence the landing scripts call.

`refs/skykeep/integrator-lease` is one JSON document — host, session, epoch,
renewed_at — written through refbus compare-and-swap. The holder refreshes it
when its named session has a live proof. A session's own declaration with a
live pid is proof. A sub-agent has none of its own: a host declaration that
names `seat=<session>` and a live pid is proof, and so is the seat file
naming `host_session=` at that host. Missing is dead. This module never
writes a declaration; a fabricated file is not a proof.

Freshness gates challenge, and challenge also refuses while that proof is
still alive, so a seat that cannot refresh itself is not taken out from
under its own work. A caller that does not name the declaration directory
has not looked, and does not take a lease that already exists. An absent
ref is epoch 1. The loser of a race stands down. The winner records the
takeover on its own status ref and in the previous holder's inbox (stdout
when that write cannot be named or delivered), then builds the sonnet-class
resume command. Fencing reads the ref fresh and refuses every merge, land
and trunk push whose host, session and epoch are not the document's. Age
does not fence the holder: a stale document still names them, and `holder`
is what reports the age.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import socket
import sys
import threading
import time
import types
from pathlib import Path
from typing import Any, NamedTuple

HERE = Path(__file__).resolve().parent
LEASE_NAME = "integrator-lease"
#: A lease this age or younger cannot be challenged. Older than this can.
LEASE_TTL_SECONDS = 5 * 60
REFBUS_MODULE_NAME = "skykeep_skybus_refbus"
#: Present only after refbus.py has finished executing. A published-but-unexecuted
#: module has none of them; a test double that stands in for refbus has all three.
_REFBUS_READY = ("read_ref", "RefBusError", "append_note")
_REFBUS = None
INBOX_MODULE_NAME = "skykeep_skybus_inbox"
_INBOX_READY = ("append_stamped", "InboxRefused")
_INBOX = None


class LeaseError(Exception):
    """The lease operation cannot go ahead. Nothing was claimed."""


class FenceError(Exception):
    """This process is not the lease holder the fresh fetch names."""

    def __init__(self, message: str, holder: dict | None = None) -> None:
        super().__init__(message)
        self.holder = holder


class RenewResult(NamedTuple):
    wrote: bool
    reason: str


class ChallengeResult(NamedTuple):
    outcome: str
    epoch: int | None
    holder: dict | None
    promote: list | None
    promote_error: str | None


def _module_ready(module, ready: tuple[str, ...]) -> bool:
    """True when `module` has finished executing, not merely been published."""
    return module is not None and all(hasattr(module, name) for name in ready)


#: `threading.Lock` is a factory, not a type, so isinstance needs the instance type.
_LOCK_TYPE = type(threading.Lock())


def _load_lock(module_name: str) -> threading.Lock:
    """One lock per module name, shared by every script that loads that name.

    The lock object lives on a sentinel module so four scripts that do not
    import each other still serialise the same `sys.modules` key. `setdefault`
    keeps a single winner when two threads install the sentinel together.
    """
    key = module_name + ".load-lock"
    current = sys.modules.get(key)
    lock = getattr(current, "lock", None)
    if isinstance(lock, _LOCK_TYPE):
        return lock
    holder = types.ModuleType(key)
    holder.lock = threading.Lock()
    stored = sys.modules.setdefault(key, holder)
    return stored.lock


def _load_refbus():
    spec = importlib.util.spec_from_file_location(REFBUS_MODULE_NAME, HERE / "refbus.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _refbus():
    """The refbus module. Tests replace this function to stand a bare repo in.

    A name present in `sys.modules` is not enough: the publisher inserts the
    module before executing it. Cache it only once the ready attributes exist,
    and otherwise load it under the shared lock so a peer cannot record the
    half-built module into its own global.
    """
    global _REFBUS
    if _module_ready(_REFBUS, _REFBUS_READY):
        return _REFBUS
    with _load_lock(REFBUS_MODULE_NAME):
        if _module_ready(_REFBUS, _REFBUS_READY):
            return _REFBUS
        cached = sys.modules.get(REFBUS_MODULE_NAME)
        if _module_ready(cached, _REFBUS_READY):
            _REFBUS = cached
            return _REFBUS
        _REFBUS = _load_refbus()
        return _REFBUS


def _inbox():
    """The inbox module: the one writer that stamps an id on a note.

    Loaded like `_refbus`, under the shared lock and cached only once finished.
    """
    global _INBOX
    if _module_ready(_INBOX, _INBOX_READY):
        return _INBOX
    with _load_lock(INBOX_MODULE_NAME):
        if _module_ready(_INBOX, _INBOX_READY):
            return _INBOX
        cached = sys.modules.get(INBOX_MODULE_NAME)
        if _module_ready(cached, _INBOX_READY):
            _INBOX = cached
            return _INBOX
        spec = importlib.util.spec_from_file_location(INBOX_MODULE_NAME, HERE / "inbox.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _INBOX = module
        return _INBOX


def _parse_decl(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        fields[key.strip()] = value.strip()
    return fields


def _pid_alive(fields: dict[str, str]) -> bool:
    """True only when `pid=` names a live process (and `socket=` a socket, if set)."""
    pid = fields.get("pid")
    if pid is None:
        return False
    try:
        pid_i = int(pid)
    except ValueError:
        return False
    try:
        os.kill(pid_i, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    except OSError:
        return False
    sock = fields.get("socket")
    if not sock:
        return True
    try:
        return Path(sock).is_socket()
    except OSError:
        return False


def _one_segment(name: str) -> bool:
    """A declaration filename stem: one path segment, never a traversal."""
    if not name or name in {".", ".."}:
        return False
    return "/" not in name and "\\" not in name and "\x00" not in name


def _names_seat(fields: dict[str, str], session: str) -> bool:
    return fields.get("seat", "").strip() == session


def _read_decl(path: Path) -> dict[str, str] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return _parse_decl(text)


def _host_decl_covers(decl_dir, host: str, session: str) -> bool:
    """The named host's own file has a live pid and declares this seat."""
    if not _one_segment(host) or host == session:
        return False
    fields = _read_decl(Path(decl_dir) / f"{host}.decl")
    if fields is None or not _pid_alive(fields):
        return False
    return _names_seat(fields, session)


def _live_decl_names_seat(decl_dir, session: str) -> bool:
    """Some other `*.decl` directly in the directory names this seat and is live.

    `done/` is a different question and is not walked. The seat's own file is
    not a substitute for a host: a missing own file is what a sub-agent has.
    """
    root = Path(decl_dir)
    try:
        entries = list(root.iterdir())
    except OSError:
        return False
    own_name = f"{session}.decl"
    for path in entries:
        if path.name == own_name or not path.name.endswith(".decl"):
            continue
        try:
            if not path.is_file():
                continue
        except OSError:
            continue
        fields = _read_decl(path)
        if fields is None:
            continue
        if _names_seat(fields, session) and _pid_alive(fields):
            return True
    return False


def session_alive(decl_dir, session: str) -> bool:
    """Whether this seat has a live proof. Missing is dead.

    The seat's own `<session>.decl` with a live pid is proof. A dead pid there
    is dead, and another file does not rescue it. With no pid, `host_session=`
    delegates to that host's declaration, which must itself have a live pid and
    `seat=<session>`. With no file at all, any other `*.decl` directly in the
    directory that names the seat and a live pid is the host declaring on the
    seat's behalf.
    """
    own = _read_decl(Path(decl_dir) / f"{session}.decl")
    if own is not None:
        if own.get("pid"):
            return _pid_alive(own)
        host = (own.get("host_session") or "").strip()
        if host:
            return _host_decl_covers(decl_dir, host, session)
        return False
    return _live_decl_names_seat(decl_dir, session)


def _is_stale(doc: dict, now: float) -> bool:
    """Older than the TTL, or a renewed_at that cannot be read. The boundary is fresh."""
    age = _age(doc.get("renewed_at"), now)
    if age is None:
        return True
    return age > LEASE_TTL_SECONDS


def stop_renew_requested(decl_dir, session: str, epoch) -> bool:
    """Whether checkup's `lease-stop-renew` marker names this session and epoch.

    A marker for another session or another epoch is not this seat's stop.
    Unreadable is not a stop: the declaration directory is the session's own.
    """
    path = Path(decl_dir) / "lease-stop-renew"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    fields = _parse_decl(text)
    if fields.get("session") != session or epoch is None:
        return False
    return fields.get("epoch") == str(epoch)


def _iso(when: float) -> str:
    return dt.datetime.fromtimestamp(when, tz=dt.UTC).isoformat()


def _age(renewed_at: Any, now: float) -> float | None:
    if not isinstance(renewed_at, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(renewed_at)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return now - parsed.timestamp()


def _note(host: str, session: str, epoch: int, previous_host: str | None) -> dict[str, Any]:
    return {
        "kind": "takeover",
        "host": host,
        "session": session,
        "epoch": epoch,
        "previous_host": previous_host,
    }


def _write_status(rb, remote: str, host: str, note: dict, root) -> None:
    """Append `note` to `status/<host>`, keeping whatever else the document holds."""
    name = f"status/{host}"
    last: Exception | None = None
    for _ in range(5):
        current = rb.read_ref(remote, name, root=root)
        if current is None:
            doc: dict[str, Any] = {"host": host, "notes": [note]}
            expected = None
        else:
            doc, expected = current
            notes = doc.get("notes")
            if not isinstance(notes, list):
                notes = []
            doc = {**doc, "notes": [*notes, note]}
        try:
            rb.cas_write_ref(remote, name, doc, expected, root=root)
            return
        except rb.RefRaced as exc:
            last = exc
    assert last is not None
    raise last


def _write_inbox(rb, remote: str, old_host: str | None, note: dict, root) -> None:
    """Tell the previous holder. A name refbus cannot carry is printed instead.

    The note goes through the inbox's stamping writer, so it carries the next
    monotonic id and the poller delivers it rather than skipping it.
    """
    if not isinstance(old_host, str) or not old_host:
        return
    inbox = _inbox()
    try:
        inbox.append_stamped(rb, remote, old_host, note, root=root)
    except (rb.RefBusError, inbox.InboxRefused):
        print(
            "integrator takeover (inbox unreachable, stdout fallback): "
            + json.dumps(note, sort_keys=True),
            flush=True,
        )


def promote_argv(epoch: int, *, environ: dict[str, str] | None = None) -> list[str]:
    """The watchdog resume command for the new seat: flock, then sonnet.

    `claude` and the lock path come from the environment (ADR-0002). A missing
    one refuses. The command is not a bare `claude -p`: flock is the wrapper
    the watchdog already uses, and the model is sonnet.
    """
    env = os.environ if environ is None else environ
    claude = (env.get("SKYKEEP_CLAUDE") or "").strip()
    lock = (env.get("SKYKEEP_INTEGRATOR_LOCK") or "").strip()
    if not claude or not lock:
        raise LeaseError(
            "promote is not configured: set SKYKEEP_CLAUDE and SKYKEEP_INTEGRATOR_LOCK"
            " (ADR-0002: the resume command is configuration, never a literal)"
        )
    prompt = (
        "You are the integrator session for this box. "
        f"The lease epoch is {epoch}. The seat is sonnet-class. "
        "A status file that names only a model identifies nobody."
    )
    return ["flock", "-n", lock, claude, "-p", prompt, "--model", "sonnet"]


def _promote(epoch: int, runner, environ):
    try:
        argv = promote_argv(epoch, environ=environ)
    except LeaseError as exc:
        return None, str(exc)
    if runner is not None:
        runner(argv)
    return argv, None


def holder(remote: str | None, *, root=None, clock=time.time) -> dict | None:
    """The lease document plus `stale`, or None when the ref is absent.

    `stale` is computed against the TTL. It is not stored: a renew writes the
    document it read, and a computed flag must not ride back into the ref.
    """
    current = _refbus().read_ref(remote, LEASE_NAME, root=root)
    if current is None:
        return None
    doc = current.doc
    if not isinstance(doc, dict):
        return None
    reported = dict(doc)
    reported["stale"] = _is_stale(doc, clock())
    return reported


def renew(remote: str | None, *, host: str, session: str, decl_dir, root=None,
          clock=time.time) -> RenewResult:
    """Refresh renewed_at when this host and session hold a live seat. Never bumps epoch."""
    if not session_alive(decl_dir, session):
        return RenewResult(False, "session-dead")
    rb = _refbus()
    for _ in range(5):
        current = rb.read_ref(remote, LEASE_NAME, root=root)
        if current is None:
            return RenewResult(False, "no-lease")
        doc, sha = current
        if doc.get("host") != host or doc.get("session") != session:
            return RenewResult(False, "not-holder")
        if stop_renew_requested(decl_dir, session, doc.get("epoch")):
            return RenewResult(False, "stop-renew")
        updated = {**doc, "renewed_at": _iso(clock())}
        try:
            rb.cas_write_ref(remote, LEASE_NAME, updated, sha, root=root)
            return RenewResult(True, "renewed")
        except rb.RefRaced:
            continue
    return RenewResult(False, "stood-down")


def _held_epoch(doc: dict) -> int | None:
    held = doc.get("epoch")
    return held if isinstance(held, int) else None


def challenge(remote: str | None, *, host: str, session: str, decl_dir=None, root=None,
              clock=time.time, runner=None, environ=None) -> ChallengeResult:
    """Claim a stale lease whose holder has no live proof, or epoch 1 when none exists.

    One CAS; a loss stands down. A lease inside the five-minute window is
    `refused-fresh` and not written. Past that window, `session_alive` on the
    holder's session is `refused-alive`: the timestamp is not the only gate,
    because a sub-agent seat cannot refresh it. A missing declaration
    directory is `refused-unproven` — the caller has not looked, and an
    unproven holder is not taken. An unreadable renewed_at is not fresh.
    Nothing here writes a declaration.
    """
    rb = _refbus()
    now = clock()
    current = rb.read_ref(remote, LEASE_NAME, root=root)
    old_host: str | None = None
    if current is None:
        epoch = 1
        expected = None
        outcome = "created"
    else:
        doc, expected = current
        age = _age(doc.get("renewed_at"), now)
        if age is not None and age <= LEASE_TTL_SECONDS:
            return ChallengeResult("refused-fresh", _held_epoch(doc), doc, None, None)
        if decl_dir is None:
            return ChallengeResult("refused-unproven", _held_epoch(doc), doc, None, None)
        held_session = doc.get("session")
        if isinstance(held_session, str) and session_alive(decl_dir, held_session):
            return ChallengeResult("refused-alive", _held_epoch(doc), doc, None, None)
        previous = doc.get("epoch")
        epoch = (previous if isinstance(previous, int) else 0) + 1
        if isinstance(doc.get("host"), str):
            old_host = doc["host"]
        outcome = "won"
    new = {"host": host, "session": session, "epoch": epoch, "renewed_at": _iso(now)}
    try:
        rb.cas_write_ref(remote, LEASE_NAME, new, expected, root=root)
    except rb.RefRaced:
        return ChallengeResult("stood-down", None, None, None, None)
    note = _note(host, session, epoch, old_host)
    # The claim has landed. The note is what the other box reads, so it is
    # written even when the resume command is not configured.
    _write_status(rb, remote, host, note, root)
    _write_inbox(rb, remote, old_host, note, root)
    promote, err = _promote(epoch, runner, environ)
    return ChallengeResult(outcome, epoch, new, promote, err)


def _this_seat(host, session, epoch):
    if host is None:
        host = (os.environ.get("SKYKEEP_HOSTNAME") or "").strip() or socket.gethostname()
    if session is None:
        session = (os.environ.get("SKYKEEP_INTEGRATOR_SESSION") or "").strip() or None
    if epoch is None:
        raw = (os.environ.get("SKYKEEP_INTEGRATOR_EPOCH") or "").strip()
        if not raw:
            epoch = None
        else:
            try:
                epoch = int(raw)
            except ValueError:
                epoch = None
    if not session or not isinstance(epoch, int):
        raise FenceError(
            "this process names no integrator session and epoch"
            " (SKYKEEP_INTEGRATOR_SESSION, SKYKEEP_INTEGRATOR_EPOCH)",
            None,
        )
    return host, session, epoch


def create_command(remote: str | None, session: str) -> str:
    """The one command that writes epoch 1 when the lease ref is absent."""
    name = remote if remote else "<remote>"
    return f"python3 scripts/agents/skybus/lease.py challenge --remote {name} --session {session}"


def fence(remote: str | None, *, root=None, host=None, session=None, epoch=None,
          clock=time.time) -> dict:
    """Fresh read. The document must name this host, session and epoch.

    Age is not a fence. A matching document is the holder, including when
    `renewed_at` is older than the TTL: challenge is what refuses to take a
    live seat, and `holder` is what reports the age. `clock` is accepted so
    callers share one clock with `holder`; fence does not read it.
    """
    del clock
    host, session, epoch = _this_seat(host, session, epoch)
    rb = _refbus()
    try:
        current = rb.read_ref(remote, LEASE_NAME, root=root)
    except rb.RefBusError as exc:
        raise FenceError(f"integrator lease cannot be read: {exc}", None) from exc
    doc = current.doc if current is not None else None
    if (
        isinstance(doc, dict)
        and doc.get("host") == host
        and doc.get("session") == session
        and doc.get("epoch") == epoch
    ):
        return doc
    if isinstance(doc, dict):
        held = f"is held by host={doc.get('host')} session={doc.get('session')} epoch={doc.get('epoch')}"
    else:
        held = (
            "is absent (no holder). Create it once with "
            f"`{create_command(remote, session)}`, export SKYKEEP_INTEGRATOR_SESSION and "
            "SKYKEEP_INTEGRATOR_EPOCH from the document it writes. "
            "Do not enable skykeep-integrator-lease.timer: a sub-agent seat has no "
            "declaration of its own, so that unit cannot renew it. A live seat is "
            "kept by challenge, which refuses while the holder's declaration is alive."
        )
    raise FenceError(
        f"integrator lease {held}; this process is host={host} session={session} epoch={epoch}",
        doc if isinstance(doc, dict) else None,
    )


def main(argv: list[str] | None = None) -> int:
    env = os.environ
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=("renew", "challenge", "holder"))
    parser.add_argument("--remote", default=env.get("SKYKEEP_GIT_REMOTE") or None)
    parser.add_argument("--host", default=env.get("SKYKEEP_HOSTNAME") or None)
    parser.add_argument("--session", default=env.get("SKYKEEP_INTEGRATOR_SESSION") or None)
    parser.add_argument("--decl-dir", default=env.get("SKYKEEP_SESSION_DECL_DIR") or None)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if not args.remote:
        parser.error("not configured: --remote or SKYKEEP_GIT_REMOTE (ADR-0002)")
    host = args.host or socket.gethostname()
    if args.mode == "holder":
        print(json.dumps(holder(args.remote), sort_keys=True, indent=2))
        return 0
    if not args.session:
        parser.error("not configured: --session or SKYKEEP_INTEGRATOR_SESSION")
    if args.mode == "renew":
        if not args.decl_dir:
            parser.error("not configured: --decl-dir or SKYKEEP_SESSION_DECL_DIR")
        result = renew(args.remote, host=host, session=args.session, decl_dir=args.decl_dir)
        print(result.reason)
        return 0
    if not args.decl_dir:
        parser.error("not configured: --decl-dir or SKYKEEP_SESSION_DECL_DIR")
    result = challenge(
        args.remote, host=host, session=args.session, decl_dir=args.decl_dir,
    )
    print(result.outcome)
    if result.promote_error:
        print(result.promote_error, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
