"""Pinned, local attempt closeout policy and append-only evidence journal.

This module supplies policy predicates only. A ``True`` result does not grant
claim, reservation, admission, launch, or other execution authority. It does
not launch or stop a process and cannot prove physical termination. A caller
must supply authenticated reconciliation evidence before using the
``server_reconcile`` or ``exposure_reconciled`` events.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from uuid import uuid4


class CloseoutError(ValueError):
    """Invalid, conflicting, or unsafe closeout journal operation."""


MAX_EVENTS = 4096
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_EVENT_BYTES = 4096
MAX_GRACE_SECONDS = 3600
MAX_CLOCK_DELTA_SECONDS = 5
HASH = re.compile(r"[0-9a-f]{64}\Z")
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
POLICIES = {"finish-current-task", "checkpoint-stop"}
EVENTS = {
    "contact_lost", "contact_restored", "clock_sample", "server_reconcile",
    "checkpoint", "process_observed", "operator_stop_requested",
    "operator_stop_observed", "exposure_reconciled",
}


def _json_bytes(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False).encode("ascii")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise CloseoutError("Journal values must be bounded JSON") from None


def _identifier(value, field):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise CloseoutError(f"{field} must be a bounded identifier")


@dataclass(frozen=True)
class AttemptPin:
    project_id: str
    task_id: str
    attempt_id: str
    claim_fence: int
    source_head: str
    input_digest: str
    approval_id: str
    cutoff_epoch: int
    invocation_id: str
    policy_version: str
    grace_seconds: int = 300
    outage_policy: str = "finish-current-task"

    def __post_init__(self):
        for name in ("project_id", "task_id", "attempt_id", "approval_id", "invocation_id"):
            _identifier(getattr(self, name), name)
        if type(self.claim_fence) is not int or not 0 < self.claim_fence < 2**63:
            raise CloseoutError("claim_fence must be a positive bigint")
        if not isinstance(self.source_head, str) or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", self.source_head):
            raise CloseoutError("source_head must be a Git SHA-1 or SHA-256 commit ID")
        if not isinstance(self.input_digest, str) or not HASH.fullmatch(self.input_digest):
            raise CloseoutError("input_digest must be a lowercase SHA-256 digest")
        _identifier(self.policy_version, "policy_version")
        if type(self.cutoff_epoch) is not int or not 0 <= self.cutoff_epoch < 2**63:
            raise CloseoutError("cutoff_epoch must be a nonnegative epoch second")
        if type(self.grace_seconds) is not int or not 0 <= self.grace_seconds <= MAX_GRACE_SECONDS:
            raise CloseoutError("grace_seconds is outside the pinned policy bound")
        if not isinstance(self.outage_policy, str) or self.outage_policy not in POLICIES:
            raise CloseoutError("unknown pinned outage policy")

    def data(self):
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True)
class ClockObservation:
    """Current wall, monotonic, boottime and boot identity supplied by caller."""

    boot_id: str
    wall_epoch: int
    monotonic_ns: int
    boottime_ns: int

    def __post_init__(self):
        _identifier(self.boot_id, "boot_id")
        if any(type(getattr(self, name)) is not int or getattr(self, name) < 0
               for name in ("wall_epoch", "monotonic_ns", "boottime_ns")):
            raise CloseoutError("Clock observation counters must be nonnegative integers")
        if (self.wall_epoch >= 2**63 or self.monotonic_ns >= 2**127
                or self.boottime_ns >= 2**127):
            raise CloseoutError("Clock observation counters exceed the supported bound")

    def data(self):
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


def _empty(pin):
    return {"schema": 1, "pin": pin.data(), "events": []}


def _digest(event_without_hash):
    return hashlib.sha256(_json_bytes(event_without_hash)).hexdigest()


def _verify(record, pin):
    if not isinstance(record, dict) or set(record) != {"schema", "pin", "events"}:
        raise CloseoutError("Journal envelope is malformed")
    if type(record["schema"]) is not int or record["schema"] != 1 or record["pin"] != pin.data():
        raise CloseoutError("Journal pin differs from immutable attempt identity")
    events = record["events"]
    if not isinstance(events, list) or len(events) > MAX_EVENTS:
        raise CloseoutError("Journal event limit exceeded")
    previous = "0" * 64
    seen = set()
    for seq, event in enumerate(events, 1):
        if not isinstance(event, dict) or set(event) != {
                "seq", "op_id", "kind", "at", "payload", "prev_hash", "hash"}:
            raise CloseoutError("Journal event is malformed")
        body = {key: event[key] for key in ("seq", "op_id", "kind", "at", "payload", "prev_hash")}
        _identifier(event["op_id"], "op_id")
        if (type(event["seq"]) is not int or event["seq"] != seq
                or not isinstance(event["kind"], str) or event["kind"] not in EVENTS
                or type(event["at"]) is not int or not 0 <= event["at"] < 2**63
                or event["prev_hash"] != previous or event["hash"] != _digest(body)
                or event["op_id"] in seen or len(_json_bytes(event)) > MAX_EVENT_BYTES):
            raise CloseoutError("Journal sequence, hash, or operation identity is invalid")
        seen.add(event["op_id"])
        previous = event["hash"]


def reduce_events(pin, events):
    """Replay journal events into a conservative policy and evidence view."""
    state = {
        "contact": "connected", "clock_parked": False, "operator_stop": False,
        "stop_observed": False, "process": "unknown", "exposure_held": True,
        "last_effective_epoch": 0, "last_clock": None, "summaries": [],
        "conflict": False, "event_count": 0,
    }
    for event in events:
        kind, payload, at = event["kind"], event["payload"], event["at"]
        if not isinstance(payload, dict):
            raise CloseoutError("Event payload must be an object")
        if at < state["last_effective_epoch"] and kind != "clock_sample":
            raise CloseoutError("Out-of-order event cannot alter current restrictions")
        state["event_count"] += 1
        # A late/out-of-order observation never moves policy time backwards.
        effective = max(state["last_effective_epoch"], at)
        state["last_effective_epoch"] = effective
        if kind == "contact_lost":
            if payload:
                raise CloseoutError("Contact loss takes no authority-changing payload")
            state["contact"] = "lost"
        elif kind == "contact_restored":
            if payload:
                raise CloseoutError("Contact restoration takes no authority-changing payload")
            state["contact"] = "connected"
        elif kind == "clock_sample":
            required = {"boot_id", "wall_epoch", "monotonic_ns", "boottime_ns"}
            if set(payload) != required or payload.get("wall_epoch") != at:
                raise CloseoutError("Clock sample fields are invalid")
            _identifier(payload["boot_id"], "boot_id")
            if any(type(payload[key]) is not int or payload[key] < 0
                   for key in ("wall_epoch", "monotonic_ns", "boottime_ns")):
                raise CloseoutError("Clock sample counters must be nonnegative integers")
            if (payload["wall_epoch"] >= 2**63 or payload["monotonic_ns"] >= 2**127
                    or payload["boottime_ns"] >= 2**127):
                raise CloseoutError("Clock sample counters exceed the supported bound")
            previous = state["last_clock"]
            if payload["wall_epoch"] < state["last_effective_epoch"]:
                state["clock_parked"] = True
            if previous is not None:
                wall_delta = payload["wall_epoch"] - previous["wall_epoch"]
                mono_delta = (payload["monotonic_ns"] - previous["monotonic_ns"]) / 1e9
                boot_delta = (payload["boottime_ns"] - previous["boottime_ns"]) / 1e9
                if (payload["boot_id"] != previous["boot_id"] or mono_delta < 0
                        or boot_delta < 0 or wall_delta < -MAX_CLOCK_DELTA_SECONDS
                        or abs(wall_delta - mono_delta) > MAX_CLOCK_DELTA_SECONDS
                        or boot_delta - mono_delta > MAX_CLOCK_DELTA_SECONDS):
                    state["clock_parked"] = True
            state["last_clock"] = payload.copy()
        elif kind == "server_reconcile":
            expected = {"approval_id": pin.approval_id, "cutoff_epoch": pin.cutoff_epoch,
                        "claim_fence": pin.claim_fence, "source_head": pin.source_head}
            if (any(payload.get(key) != value for key, value in expected.items())
                    or type(payload.get("claim_fence")) is not int
                    or type(payload.get("cutoff_epoch")) is not int
                    or set(payload) != set(expected) | {"server_now"}):
                raise CloseoutError("Server reconciliation does not match pinned authority")
            if (type(payload["server_now"]) is not int
                    or not effective <= payload["server_now"] < 2**63
                    or at != payload["server_now"]):
                raise CloseoutError("Server time cannot move the pinned policy backwards")
            state["last_effective_epoch"] = max(effective, payload["server_now"])
            state["clock_parked"] = False
        elif kind == "checkpoint":
            if set(payload) != {"kind", "artifact_ref", "summary"} or payload["kind"] not in {"checkpoint", "resume"}:
                raise CloseoutError("Checkpoint or resume summary fields are invalid")
            _identifier(payload["artifact_ref"], "artifact_ref")
            summary = payload["summary"]
            if not isinstance(summary, str) or not summary.strip():
                raise CloseoutError("Checkpoint or resume summary must be bounded text")
            try:
                if len(summary.encode("utf-8")) > 512:
                    raise CloseoutError("Checkpoint or resume summary must be bounded text")
            except UnicodeEncodeError:
                raise CloseoutError("Checkpoint or resume summary must be valid UTF-8") from None
            if len(state["summaries"]) >= 128:
                raise CloseoutError("Attempt summary limit exceeded")
            state["summaries"].append({"task_id": pin.task_id, "kind": payload["kind"],
                                       "artifact_ref": payload["artifact_ref"], "summary": summary})
        elif kind == "process_observed":
            if (set(payload) != {"invocation_id", "state"}
                    or not isinstance(payload["state"], str)
                    or payload["state"] not in {"running", "unknown", "exited"}):
                raise CloseoutError("Process observation is invalid")
            if payload["invocation_id"] != pin.invocation_id:
                state["exposure_held"] = True
                state["conflict"] = True
            elif state["process"] == "exited" and payload["state"] != "exited":
                # For one immutable invocation, terminal evidence is monotonic;
                # delayed samples cannot resurrect an exited process.
                raise CloseoutError("Terminal process evidence cannot regress")
            else:
                state["process"] = payload["state"]
                if payload["state"] != "exited":
                    state["exposure_held"] = True
        elif kind == "operator_stop_requested":
            if payload:
                raise CloseoutError("Operator stop request takes no authority-changing payload")
            state["operator_stop"] = True
        elif kind == "operator_stop_observed":
            if payload != {"invocation_id": pin.invocation_id} or not state["operator_stop"] or state["process"] != "exited":
                raise CloseoutError("Stop observation requires requested stop and exact exited invocation")
            state["stop_observed"] = True
        elif kind == "exposure_reconciled":
            if (payload != {"invocation_id": pin.invocation_id} or state["process"] != "exited"
                    or state["conflict"]):
                raise CloseoutError("Exposure release requires authenticated exact-invocation exit evidence")
            state["exposure_held"] = False
    return state


class AttemptCloseoutJournal:
    """Single-file, private, hash-chained journal protected by an advisory lock.

    The filesystem protects against accidental corruption and cross-process
    races, not a malicious process running as the same UID.
    """

    def __init__(self, directory: Path, pin: AttemptPin):
        self.directory = Path(directory)
        self.pin = pin
        if not self.directory.is_absolute():
            raise CloseoutError("Journal directory must be absolute")
        # Directory creation is explicit caller setup; do not create paths here.
        with self._locked() as dirfd:
            self._cleanup_pending(dirfd)
            try:
                raw = self._read(dirfd)
            except FileNotFoundError:
                self._write(dirfd, _empty(pin))
            else:
                record = self._decode(raw)
                _verify(record, pin)

    @staticmethod
    def _private(info, *, directory=False):
        kind = stat.S_ISDIR if directory else stat.S_ISREG
        mode = 0o700 if directory else 0o600
        if (not kind(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != mode
                or (not directory and info.st_nlink != 1)):
            raise CloseoutError("Journal paths must be private, ordinary, user-owned paths")

    @contextmanager
    def _locked(self):
        directory = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        lock = None
        try:
            self._private(os.fstat(directory), directory=True)
            lock = os.open(".closeout.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                           0o600, dir_fd=directory)
            self._private(os.fstat(lock))
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield directory
        finally:
            if lock is not None:
                os.close(lock)
            os.close(directory)

    def _read(self, dirfd):
        fd = os.open("journal.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dirfd)
        try:
            info = os.fstat(fd)
            self._private(info)
            if info.st_size > MAX_FILE_BYTES:
                raise CloseoutError("Journal exceeds byte bound")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                raw = stream.read(MAX_FILE_BYTES + 1)
            if len(raw) > MAX_FILE_BYTES:
                raise CloseoutError("Journal exceeds byte bound")
            return raw
        finally:
            os.close(fd)

    def _cleanup_pending(self, dirfd):
        removed = False
        with os.scandir(dirfd) as entries:
            for entry in entries:
                if entry.name in {".closeout.lock", "journal.json"}:
                    continue
                if not re.fullmatch(r"\.pending-[0-9a-f]{24}", entry.name):
                    raise CloseoutError("Journal directory contains an unexpected path")
                fd = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dirfd)
                try:
                    self._private(os.fstat(fd))
                finally:
                    os.close(fd)
                os.unlink(entry.name, dir_fd=dirfd)
                removed = True
        if removed:
            os.fsync(dirfd)

    @staticmethod
    def _decode(raw):
        try:
            record = json.loads(raw)
            if _json_bytes(record) != raw:
                raise ValueError
            return record
        except (ValueError, TypeError, UnicodeError):
            raise CloseoutError("Journal encoding is invalid") from None

    @staticmethod
    def _write(dirfd, record):
        raw = _json_bytes(record)
        if len(raw) > MAX_FILE_BYTES:
            raise CloseoutError("Journal exceeds byte bound")
        name = f".pending-{os.urandom(12).hex()}"
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=dirfd)
        try:
            view = memoryview(raw)
            while view:
                count = os.write(fd, view)
                if count <= 0:
                    raise OSError("short journal write")
                view = view[count:]
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            try:
                os.unlink(name, dir_fd=dirfd)
            except FileNotFoundError:
                pass
            raise
        else:
            os.close(fd)
        os.rename(name, "journal.json", src_dir_fd=dirfd, dst_dir_fd=dirfd)
        os.fsync(dirfd)

    def append(self, op_id, kind, at, payload=None, *, expected_sequence=None):
        _identifier(op_id, "op_id")
        if (not isinstance(kind, str) or kind not in EVENTS or type(at) is not int
                or not 0 <= at < 2**63):
            raise CloseoutError("Event kind and timestamp are invalid")
        payload = {} if payload is None else payload
        if not isinstance(payload, dict) or len(_json_bytes(payload)) > MAX_EVENT_BYTES:
            raise CloseoutError("Event payload exceeds its bound")
        payload = json.loads(_json_bytes(payload))
        if expected_sequence is not None and (type(expected_sequence) is not int or expected_sequence < 0):
            raise CloseoutError("Expected sequence must be a nonnegative integer")
        with self._locked() as dirfd:
            record = self._decode(self._read(dirfd))
            _verify(record, self.pin)
            events = record["events"]
            body = {"op_id": op_id, "kind": kind, "at": at, "payload": payload}
            for prior in events:
                if prior["op_id"] == op_id:
                    if any(prior[key] != body[key] for key in body):
                        raise CloseoutError("Operation ID was replayed with a different payload")
                    return reduce_events(self.pin, events)
            if expected_sequence is not None and expected_sequence != len(events):
                raise CloseoutError("Journal sequence changed; retry from fresh state")
            if len(events) >= MAX_EVENTS:
                raise CloseoutError("Journal event limit exceeded")
            reduce_events(self.pin, events)
            event = {"seq": len(events) + 1, **body,
                     "prev_hash": events[-1]["hash"] if events else "0" * 64}
            event["hash"] = _digest(event)
            next_events = [*events, event]
            # Validate semantics before the durable replace.
            reduce_events(self.pin, next_events)
            if len(_json_bytes(event)) > MAX_EVENT_BYTES:
                raise CloseoutError("Event exceeds its byte bound")
            record["events"] = next_events
            self._write(dirfd, record)
            return reduce_events(self.pin, next_events)

    def snapshot(self):
        with self._locked() as dirfd:
            record = self._decode(self._read(dirfd))
            _verify(record, self.pin)
            return reduce_events(self.pin, record["events"])

    def _record_decision_clock(self, observation):
        if not isinstance(observation, ClockObservation):
            raise CloseoutError("A current ClockObservation is required for every policy decision")
        self.append(f"clock-{uuid4().hex}", "clock_sample", observation.wall_epoch,
                    observation.data())
        state = self.snapshot()
        effective_now = max(observation.wall_epoch, state["last_effective_epoch"])
        return state, effective_now

    def may_admit_new_task(self, observation):
        state, now = self._record_decision_clock(observation)
        return (now < self.pin.cutoff_epoch
                and state["contact"] == "connected" and not state["clock_parked"]
                and not state["operator_stop"] and not state["conflict"])

    def may_call_model(self, observation, *, purpose="task"):
        if not isinstance(purpose, str) or purpose not in {"task", "closeout"}:
            raise CloseoutError("Model call purpose must be task or closeout")
        state, now = self._record_decision_clock(observation)
        if (state["clock_parked"] or state["operator_stop"] or state["conflict"]
                or state["process"] != "running"):
            return False
        cutoff = self.pin.cutoff_epoch
        grace_end = cutoff + self.pin.grace_seconds
        if purpose == "task":
            return (now < cutoff and
                    (state["contact"] == "connected" or self.pin.outage_policy == "finish-current-task"))
        if purpose == "closeout":
            return cutoff <= now < grace_end
        return False
