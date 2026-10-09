"""The todo service's client, and the feed the session keeper drives with it.

Design: `docs/planning/todo-service.md` §§ 5 and 6. The token is read from a mode-0600 file and
travels in the `Authorization` header only: never in a URL, a body, a log line or an exception.
The base URL comes from the caller's configuration (ADR-0002) and is refused if it could carry a
credential. Redirects are never followed, so the header is never replayed to another host.

Three failures are kept apart because the journal must say which it was:

- `Unreachable` and `TokenRefused`: the service cannot serve this box. Nothing starts: the service
  is the only source of items, so there is no other place to take one from (fail closed).
- `Malformed`: the service answered something this client does not know. Nothing starts: an
  unknown reply is not a reason to guess.
- `ClaimLost`: the lease lapsed and the item may be another box's now. The late post is refused
  by the service and dropped here.
"""

from __future__ import annotations

import json
import re
import stat
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlsplit

#: (method, url, headers, body, timeout seconds) -> (status, body). Raises OSError when the
#: service cannot be reached.
Transport = Callable[[str, str, Mapping[str, str], bytes | None, float], tuple[int, bytes]]

ID_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9-]{0,199}$")
LANE_LINE = re.compile(r"^Lane: .+$", re.MULTILINE)

ITEM = "item"
NOTHING = "nothing"
#: What a pending done records: enough to post it again unchanged.
DONE_FIELDS = ("seam", "tip_sha", "lane")


class ClientError(Exception):
    """Any failure of a call. The message never holds the token."""


class ClientConfigError(ClientError):
    """The URL or the token file is unusable: the mode is refused before any call is made."""


class Unreachable(ClientError):
    """No answer, or a server error."""


class TokenRefused(ClientError):
    """The service does not accept this box's token."""


class Malformed(ClientError):
    """An answer of an unknown status or shape."""


class ClaimLost(ClientError):
    """The claim is no longer live: the lease lapsed, or the item was released or revoked."""


class Rejected(ClientError):
    """The service refused the request by name (`code` is its `error` field)."""

    def __init__(self, code: str) -> None:
        super().__init__(f"the service refused the request: {code}")
        self.code = code


def read_token(path: str | Path) -> str:
    """The token in a private file: one word. A loose, missing, empty or multi-word file refuses."""
    p = Path(path)
    try:
        mode = stat.S_IMODE(p.stat().st_mode)
        text = p.read_text()
    except (OSError, ValueError) as exc:
        raise ClientConfigError(f"token file unreadable: {type(exc).__name__}") from None
    if mode & 0o077:
        raise ClientConfigError("token file must not be readable by group or other")
    words = text.split()
    if len(words) != 1:
        raise ClientConfigError("token file must hold exactly one token")
    return words[0]


def check_url(url: str) -> str:
    """The base URL without a trailing slash; refused if it is not plain http(s) to a host."""
    try:
        parts = urlsplit(url.strip())
        port_ok = parts.port is None or parts.port > 0
    except ValueError:
        raise ClientConfigError("the service URL does not parse") from None
    if parts.scheme not in ("http", "https") or not parts.hostname or not port_ok:
        raise ClientConfigError("the service URL must be http(s)://host[:port][/path]")
    if parts.username is not None or parts.password is not None:
        raise ClientConfigError("the service URL must not carry a credential")
    if parts.query or parts.fragment or "?" in url or "#" in url:
        raise ClientConfigError("the service URL must not carry a query or a fragment")
    return url.strip().rstrip("/")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def http_transport(method: str, url: str, headers: Mapping[str, str], body: bytes | None,
                   timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except urllib.error.URLError as exc:
        raise OSError(type(exc.reason).__name__) from None


#: The verbs a `last_note` may carry, and the shape of its time: both go into a session's prompt
#: outside the fence, so anything else drops the note.
LAST_NOTE_VERBS = ("bounce", "block", "release")
LAST_NOTE_TIME = re.compile(r"^[0-9T:.+Z-]{1,40}$")


def last_note_of(item: dict) -> dict | None:
    """The item's `last_note` as {verb, at, note}, or None when the reply has none (a fresh item, an
    older service) or it is not that shape. Never a reason to refuse the claim: the session starts
    without it. The note itself is untrusted text and is passed on unread."""
    last = item.get("last_note")
    if not isinstance(last, dict):
        return None
    verb, at, note = last.get("verb"), last.get("at"), last.get("note")
    if verb not in LAST_NOTE_VERBS or not isinstance(at, str) or not LAST_NOTE_TIME.match(at):
        return None
    if not isinstance(note, str) or not note.strip():
        return None
    return {"verb": verb, "at": at, "note": note}


@dataclass(frozen=True)
class Claim:
    """One item this box holds: what `heartbeat`, `done` and `release` name."""
    item_id: str
    claim_id: int
    lease_until: str
    #: The item's body as the service sent it with the claim (a `bash` item carries its command here).
    body: str = field(default="", compare=False)
    #: The note that last sent the item back ({verb, at, note}), or None: `last_note` of the claim's reply.
    last_note: dict | None = field(default=None, compare=False)


class TodoClient:
    def __init__(self, base_url: str, token: str, *, timeout_seconds: float,
                 transport: Transport | None = None) -> None:
        self.base_url = check_url(base_url)
        if not token or token != token.strip() or len(token.split()) != 1:
            raise ClientConfigError("the token must be one word")
        if timeout_seconds <= 0:
            raise ClientConfigError("the timeout must be positive")
        self._token = token
        self._timeout = timeout_seconds
        self._transport = transport or http_transport

    def _post(self, path: str, body: Mapping[str, object] | None, *, method: str = "POST") -> tuple[int, object]:
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            status, raw = self._transport(method, self.base_url + path, headers,
                                          None if body is None else json.dumps(body).encode(), self._timeout)
        except OSError as exc:
            raise Unreachable(f"no answer ({type(exc).__name__})") from None
        if status in (401, 403):
            raise TokenRefused(f"HTTP {status}")
        if status >= 500:
            raise Unreachable(f"HTTP {status}")
        if status == 204:
            return status, None
        try:
            data = json.loads(raw)
        except ValueError:
            raise Malformed(f"HTTP {status} with a body that is not JSON") from None
        if status == 200:
            return status, data
        code = data.get("error") if isinstance(data, dict) else None
        if not 400 <= status < 500 or not isinstance(code, str) or not code:
            raise Malformed(f"HTTP {status}")
        if code == "claim_lost":
            raise ClaimLost(code)
        raise Rejected(code)

    def next(self, *, box: str, models: list[str], agent: str, session: str = "keeper",
             worktree: str | None = None, lane: int | None = None,
             stack_ok: list[str] | None = None) -> Claim | None:
        """Claim the best item whose brief model is one of `models`, or None when nothing is eligible."""
        if not models or not all(isinstance(m, str) and m for m in models):
            raise ClientConfigError("a take names at least one brief model")
        _, data = self._post("/items/next", {"models": list(models), "agent": agent, "session": session,
                                             "worktree": worktree, "lane": lane, "stack_ok": stack_ok})
        if data is None:
            return None
        item = data.get("item") if isinstance(data, dict) else None
        claim = data.get("claim") if isinstance(data, dict) else None
        if not isinstance(item, dict) or not isinstance(claim, dict):
            raise Malformed("a reply without an item and a claim")
        item_id, claim_id, lease = item.get("id"), claim.get("claim_id"), claim.get("lease_until")
        if not isinstance(item_id, str) or not ID_PATTERN.match(item_id):
            raise Malformed("a reply whose item id is not a row id")
        if type(claim_id) is not int or not isinstance(lease, str) or not lease:
            raise Malformed("a reply whose claim has no id or no lease")
        if item.get("model") not in models or claim.get("box") != box:
            # The service claimed the item before it replied: give the claim back, or it is held
            # from every other box until its lease lapses and counts toward the handover. Best
            # effort: the reply is refused either way.
            try:
                self.release(Claim(item_id, claim_id, lease), note="the reply named another model or box")
                kept = "the claim was given back"
            except ClientError as exc:
                kept = f"the claim could not be given back ({exc}); its lease ends it"
            raise Malformed(f"a reply for another model or another box; {kept}")
        body = item.get("body")
        return Claim(item_id, claim_id, lease, body if isinstance(body, str) else "", last_note_of(item))

    def item(self, item_id: str) -> dict:
        """`GET /items/<Id>`: the item with its `sent_back` and `review` (what a session starting on it
        must read first). An id that is no row id is refused before any call."""
        if not ID_PATTERN.match(item_id or ""):
            raise ClientConfigError("an item id is a row id")
        _, data = self._post(f"/items/{item_id}", None, method="GET")
        if not isinstance(data, dict) or data.get("id") != item_id:
            raise Malformed("an item reply that is not the item asked for")
        return data

    def heartbeat(self, claim: Claim) -> str:
        _, data = self._post(f"/items/{claim.item_id}/heartbeat", {"claim_id": claim.claim_id})
        lease = data.get("lease_until") if isinstance(data, dict) else None
        if not isinstance(lease, str) or not lease:
            raise Malformed("a heartbeat reply without a lease")
        return lease

    def done(self, claim: Claim, *, seam: str, tip_sha: str, lane_evidence: str,
             base_sha: str | None = None, notes: str | None = None) -> None:
        self._post(f"/items/{claim.item_id}/done", {
            "claim_id": claim.claim_id, "seam": seam, "base_sha": base_sha, "tip_sha": tip_sha,
            "lane_evidence": lane_evidence, "notes": notes})

    def block(self, claim: Claim, *, reason: str, blocked_on: list[str] | None = None) -> None:
        """Post that the item cannot be done: the reason, and the item ids it waits on (if any)."""
        self._post(f"/items/{claim.item_id}/block", {
            "claim_id": claim.claim_id, "reason": reason, "blocked_on": blocked_on or []})

    def finish(self, claim: Claim, *, exit_code: int, output: str) -> None:
        """Post the end of a `bash` item's command: exit 0 lands it, any other exit blocks it."""
        self._post(f"/items/{claim.item_id}/finish", {
            "claim_id": claim.claim_id, "exit_code": exit_code, "output": output})

    def release(self, claim: Claim, *, note: str | None = None) -> None:
        self._post(f"/items/{claim.item_id}/release", {
            "claim_id": claim.claim_id, "reason": "released", "note": note})


def lane_line(tip_message: str) -> str | None:
    """The tip commit's `Lane:` trailer line, or None. The service judges its grammar."""
    found = LANE_LINE.findall(tip_message or "")
    return found[-1].strip() if found else None


@dataclass(frozen=True)
class Take:
    """What a free slot got: an item to start, or nothing."""
    kind: str
    row: str | None = None
    claim_id: int | None = None
    why: str = ""
    # NOTHING because the service gave no usable answer (unreachable, token refused, reply not
    # understood), not because no item is eligible: asking again in the same tick is pointless.
    unknown: bool = False
    #: The note that last sent the item back ({verb, at, note}), or None (a fresh item, an older service).
    last_note: dict | None = None


class KeeperFeed:
    """The keeper's side of the service: one claim per slot, held from `take` to `finish`.

    Every outcome is journalled through `log`. No method raises for a failed call: a keeper tick
    must outlive the service.
    """

    def __init__(self, client: TodoClient, *, box: str, log: Callable[[str], None]) -> None:
        self.client = client
        self.box = box
        self.log = log
        self.claims: dict[int, Claim] = {}
        #: Per slot, the done the service did not take (unreachable or a reply not understood):
        #: {seam, tip_sha, lane}. Its claim is kept and not heartbeated, so the lease bounds the retries.
        self.pending: dict[int, dict[str, str]] = {}

    def take(self, slot: int, *, models: list[str], agent: str, worktree: str | None = None,
             lane: int | None = None, stack_ok: list[str] | None = None) -> Take:
        """Ask for the slot's next item among the brief models it may take (KEEPER_TODO_MODELS)."""
        try:
            claim = self.client.next(box=self.box, models=models, agent=agent, session=f"keeper-{slot}",
                                     worktree=worktree, lane=lane, stack_ok=stack_ok)
        except TokenRefused as exc:
            return self._no_answer(slot, f"the todo service refused the token ({exc})")
        except Unreachable as exc:
            return self._no_answer(slot, f"the todo service is unreachable ({exc})")
        except ClientError as exc:
            return self._no_answer(slot, f"unknown reply from the todo service ({exc})")
        if claim is None:
            return Take(NOTHING, why="the todo service has no eligible item")
        self.claims[slot] = claim
        self.log(f"slot {slot}: took {claim.item_id} from the todo service (claim {claim.claim_id}, "
                 f"lease until {claim.lease_until})")
        return Take(ITEM, row=claim.item_id, claim_id=claim.claim_id, last_note=claim.last_note)

    def item(self, row: str) -> dict | None:
        """`GET /items/<Id>` (what a session starting on `row` must read first), or None, journalled,
        when the service does not answer it: the session then starts without it."""
        try:
            return self.client.item(row)
        except ClientError as exc:
            self.log(f"{row}: the todo service did not say what a session must read first ({exc}); "
                     "it starts without it")
            return None

    def _no_answer(self, slot: int, why: str) -> Take:
        why = f"slot {slot}: {why}; nothing starts"
        self.log(why)
        return Take(NOTHING, why=why, unknown=True)

    def beat(self, slot: int) -> bool:
        """Extend the slot's lease. False only when the claim is lost, and it is then dropped. A claim
        whose done is pending is not extended: its lease is what bounds the retries."""
        claim = self.claims.get(slot)
        if claim is None or slot in self.pending:
            return True
        try:
            lease = self.client.heartbeat(claim)
        except (ClaimLost, Rejected) as exc:
            del self.claims[slot]
            self.log(f"slot {slot}: the claim on {claim.item_id} is lost ({exc}); another box may hold it")
            return False
        except Unreachable as exc:
            self.log(f"slot {slot}: heartbeat for {claim.item_id} not taken, the todo service is unreachable ({exc})")
            return True
        except ClientError as exc:
            self.log(f"slot {slot}: heartbeat for {claim.item_id} not taken ({exc})")
            return True
        # The new lease only: the claim keeps its body and the note that sent the item back.
        self.claims[slot] = replace(claim, lease_until=lease)
        return True

    def finish(self, slot: int, *, seam: str, tip_sha: str, tip_message: str) -> bool:
        """Post the slot's item done. True only when the service recorded the seam.

        A tip without a `Lane:` line is not done: the item is released for another session. A done
        the service did not take (unreachable, a server error, a reply not understood) keeps the
        claim and is posted again by `retry`, once per keeper tick, until the service takes it,
        names a refusal, or says the claim is lost; the lapsing lease ends it at the latest.
        """
        claim = self.claims.get(slot)
        if claim is None:
            return False
        lane = lane_line(tip_message)
        if lane is None:
            self.claims.pop(slot, None)
            self.pending.pop(slot, None)
            try:
                self.client.release(claim, note="the seam tip carries no Lane evidence")
            except ClientError as exc:
                self.log(f"slot {slot}: release of {claim.item_id} not recorded ({exc}); its lease will lapse")
                return False
            self.log(f"slot {slot}: {claim.item_id} released, its tip has no Lane line")
            return False
        return self._post_done(slot, claim, {"seam": seam, "tip_sha": tip_sha, "lane": lane})

    def retry(self, slot: int) -> bool:
        """Post the slot's pending done again (at most once per tick: the keeper calls it once)."""
        claim, done = self.claims.get(slot), self.pending.get(slot)
        if claim is None or done is None:
            self.pending.pop(slot, None)
            return False
        return self._post_done(slot, claim, done)

    def _post_done(self, slot: int, claim: Claim, done: dict[str, str]) -> bool:
        try:
            self.client.done(claim, seam=done["seam"], tip_sha=done["tip_sha"], lane_evidence=done["lane"])
        except ClaimLost as exc:
            self._forget(slot)
            self.log(f"slot {slot}: done for {claim.item_id} refused, the claim is lost ({exc})")
            return False
        except Rejected as exc:
            self._forget(slot)
            self.log(f"slot {slot}: done for {claim.item_id} refused by the todo service ({exc.code})")
            return False
        except ClientError as exc:
            self.pending[slot] = dict(done)
            self.log(f"slot {slot}: done for {claim.item_id} not recorded ({exc}); the claim is kept and "
                     "the done retried at the next tick while its lease lasts")
            return False
        self._forget(slot)
        self.log(f"slot {slot}: {claim.item_id} posted done ({done['seam']} at {done['tip_sha']}; {done['lane']})")
        return True

    def _forget(self, slot: int) -> None:
        self.claims.pop(slot, None)
        self.pending.pop(slot, None)

    def block(self, slot: int, reason: str, blocked_on: list[str]) -> None:
        """Post the slot's item as blocked, with the reason: it is not handed out again until the rows it
        waits on land, or, waiting on nothing a row names, until the owner releases it."""
        claim = self.claims.pop(slot, None)
        self.pending.pop(slot, None)
        if claim is None:
            return
        try:
            self.client.block(claim, reason=reason, blocked_on=blocked_on)
        except ClientError as exc:
            self.log(f"slot {slot}: block of {claim.item_id} not recorded ({exc}); its lease will lapse")
            return
        self.log(f"slot {slot}: {claim.item_id} posted blocked"
                 f"{' on ' + ', '.join(blocked_on) if blocked_on else ''}")

    def drop(self, slot: int, note: str) -> None:
        """Give the slot's item back: the session ended without a pushed seam."""
        claim = self.claims.pop(slot, None)
        self.pending.pop(slot, None)
        if claim is None:
            return
        try:
            self.client.release(claim, note=note)
        except ClientError as exc:
            self.log(f"slot {slot}: release of {claim.item_id} not recorded ({exc}); its lease will lapse")
            return
        self.log(f"slot {slot}: {claim.item_id} released ({note})")

    def as_json(self) -> dict[str, dict[str, object]]:
        out: dict[str, dict[str, object]] = {}
        for slot, c in self.claims.items():
            out[str(slot)] = {"item_id": c.item_id, "claim_id": c.claim_id, "lease_until": c.lease_until}
            if c.last_note is not None:
                # Recorded with the claim: a keeper restart must not cost the next session its note.
                out[str(slot)]["last_note"] = dict(c.last_note)
            if slot in self.pending:
                out[str(slot)]["done"] = dict(self.pending[slot])
        return out

    def restore(self, saved: object) -> None:
        """Take back the claims, and the dones still to post, a previous keeper process recorded; an
        unreadable record is none. A recorded `last_note` of another shape costs the note, not the claim."""
        claims: dict[int, Claim] = {}
        pending: dict[int, dict[str, str]] = {}
        if not isinstance(saved, dict):
            return
        for key, value in saved.items():
            if not isinstance(value, dict) or not str(key).isdigit():
                return
            item_id, claim_id = value.get("item_id"), value.get("claim_id")
            if not isinstance(item_id, str) or not ID_PATTERN.match(item_id) or type(claim_id) is not int:
                return
            claims[int(key)] = Claim(item_id, claim_id, str(value.get("lease_until") or ""),
                                     last_note=last_note_of(value))
            if "done" in value:
                done = value["done"]
                if not isinstance(done, dict) or not all(isinstance(done.get(k), str) and done.get(k)
                                                         for k in DONE_FIELDS):
                    return
                pending[int(key)] = {k: done[k] for k in DONE_FIELDS}
        self.claims, self.pending = claims, pending
