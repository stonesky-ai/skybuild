"""Bounded, private observation delivery; never grants execution authority.

The caller supplies packet identities and a synchronous transport with its own
finite timeout. A response must match the stored evidence before removal.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from uuid import uuid4

from .contracts import DomainError, valid_identifier

IDENTITY = ("task_id", "attempt_id", "claim_fence", "component_id", "source_id",
            "boot_id", "pid", "process_start")
FIELDS = {*IDENTITY, "event_id", "source_sequence", "observed_at", "state", "evidence_refs"}
STATES = {"running", "waiting", "unknown", "exited", "interrupted", "result-reported"}
RECORD = re.compile(r"[0-9a-f]{64}\.json\Z")
TEMP = re.compile(r"\.pending-[0-9a-f]{32}\Z")


class SpoolError(ValueError):
    """Refused local state; pending evidence is not discarded."""


@dataclass(frozen=True)
class ReplayOutcome:
    event_id: str
    status: str
    response: object = None


def _encode(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise SpoolError("Observation must be valid JSON") from None


def _validate(project_id, packet):
    # This is the packet portion of Observations.record_observation; authorization,
    # reservation/fence validation and projection eligibility remain server-side.
    if not valid_identifier(project_id) or not isinstance(packet, dict) or set(packet) != FIELDS:
        raise SpoolError("Observation requires project and exact packet fields")
    for key in ("event_id", "task_id", "attempt_id", "component_id", "source_id", "boot_id"):
        if not valid_identifier(packet[key]):
            raise SpoolError("Observation contains an invalid identifier")
    for key in ("claim_fence", "pid", "source_sequence"):
        if type(packet[key]) is not int or not 0 < packet[key] < 2**63:
            raise SpoolError("Observation integers must be positive bigints")
    start = packet["process_start"]
    if not isinstance(start, str) or not start.strip() or len(start) > 200 or "\x00" in start:
        raise SpoolError("Observation requires process start identity")
    if not isinstance(packet["state"], str) or packet["state"] not in STATES:
        raise SpoolError("Observation state is not execution authority")
    refs = packet["evidence_refs"]
    if not isinstance(refs, list) or len(refs) > 8 or not all(valid_identifier(ref) for ref in refs):
        raise SpoolError("Observation requires bounded opaque evidence references")
    try:
        observed = datetime.fromisoformat(packet["observed_at"])
        if observed.tzinfo is None or observed.utcoffset() is None:
            raise ValueError
        observed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        raise SpoolError("Observation requires an offset-aware timestamp") from None


class ObservationSpool:
    def __init__(self, directory: Path, *, max_bytes=1024 * 1024, max_records=256,
                 max_packet_bytes=16384):
        self.directory = Path(directory)
        if not self.directory.is_absolute():
            raise SpoolError("Spool directory must be absolute")
        for value in (max_bytes, max_records, max_packet_bytes):
            if type(value) is not int or value <= 0:
                raise SpoolError("Spool bounds must be positive integers")
        self.max_bytes, self.max_records = max_bytes, max_records
        self.max_packet_bytes = max_packet_bytes
        # Directory creation is explicit caller setup, not an implicit side effect.
        with self._locked():
            pass

    @staticmethod
    def _private(info, *, directory=False):
        expected = 0o700 if directory else 0o600
        kind = stat.S_ISDIR if directory else stat.S_ISREG
        if (not kind(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != expected
                or (not directory and info.st_nlink != 1)):
            raise SpoolError("Spool paths must be private, ordinary, user-owned paths")

    @contextmanager
    def _locked(self):
        directory = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        lock = None
        try:
            self._private(os.fstat(directory), directory=True)
            lock = os.open(".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
            self._private(os.fstat(lock))
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield directory
        finally:
            if lock is not None:
                os.close(lock)
            os.close(directory)

    def _read(self, directory, name):
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            info = os.fstat(descriptor)
            self._private(info)
            if info.st_size > self.max_packet_bytes:
                raise SpoolError("Spool record exceeds packet bound")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                data = stream.read(self.max_packet_bytes + 1)
            if len(data) > self.max_packet_bytes:
                raise SpoolError("Spool record exceeds packet bound")
            return data
        finally:
            os.close(descriptor)

    def _records(self, directory):
        entries = os.scandir(directory)
        try:
            return self._scan(directory, entries)
        finally:
            entries.close()

    def _scan(self, directory, entries):
        records, size, scanned = [], 0, 0
        for entry in entries:
            name = entry.name
            if name == ".lock":
                continue
            scanned += 1
            if scanned > self.max_records + 1:
                raise SpoolError("Spool directory exceeds bounded file count")
            if TEMP.fullmatch(name):
                # A temporary file was never published or offered to transport.
                self._read(directory, name)
                os.unlink(name, dir_fd=directory)
                os.fsync(directory)
                continue
            if not RECORD.fullmatch(name):
                raise SpoolError("Spool contains an unexpected file")
            data = self._read(directory, name)
            try:
                envelope = json.loads(data)
                if not isinstance(envelope, dict) or set(envelope) != {"project_id", "packet", "last_outcome"}:
                    raise ValueError
                _validate(envelope["project_id"], envelope["packet"])
                outcome = envelope["last_outcome"]
                if (not isinstance(outcome, dict) or set(outcome) != {"status", "status_code"}
                        or outcome["status"] not in {"pending", "unknown", "conflict"}
                        or (outcome["status_code"] is not None and
                            (type(outcome["status_code"]) is not int or not 100 <= outcome["status_code"] <= 599))):
                    raise ValueError
                if self._name(envelope) != name or _encode(envelope) != data:
                    raise ValueError
            except (ValueError, TypeError, KeyError, UnicodeError):
                raise SpoolError("Spool record is invalid; reconciliation required") from None
            if self._charge(envelope) > self.max_packet_bytes:
                raise SpoolError("Spool record exceeds packet bound")
            size += self._charge(envelope)
            records.append((name, data, envelope))
            if len(records) > self.max_records or size > self.max_bytes:
                raise SpoolError("Existing spool exceeds configured bounds")
        return sorted(records), size

    @staticmethod
    def _name(envelope):
        key = [envelope["project_id"], envelope["packet"]["event_id"]]
        return hashlib.sha256(_encode(key)).hexdigest() + ".json"

    @staticmethod
    def _charge(envelope):
        # Reserve bounded outcome metadata before admission; retries cannot grow
        # committed storage beyond the originally admitted capacity.
        return len(_encode({"project_id": envelope["project_id"], "packet": envelope["packet"]})) + 128

    @staticmethod
    def _publish(directory, name, data):
        temporary = ".pending-" + uuid4().hex
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
        try:
            remaining = memoryview(data)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("Observation write made no progress")
                remaining = remaining[written:]
            os.fsync(descriptor)
            os.rename(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass

    def pending(self):
        """Read bounded pending packets and sanitized durable replay outcomes."""
        with self._locked() as directory:
            records, _ = self._records(directory)
            return [envelope for _, _, envelope in records]

    def enqueue(self, project_id, packet):
        """Publish immutable evidence durably before any transport can see it."""
        _validate(project_id, packet)
        data = _encode({"project_id": project_id, "packet": packet,
                        "last_outcome": {"status": "pending", "status_code": None}})
        if self._charge({"project_id": project_id, "packet": packet}) > self.max_packet_bytes:
            raise SpoolError("Observation exceeds packet bound")
        envelope = json.loads(data)  # Snapshot caller-owned lists and dictionaries.
        name = self._name(envelope)
        with self._locked() as directory:
            records, size = self._records(directory)
            for existing, _, prior in records:
                if existing == name:
                    if prior["packet"] != envelope["packet"]:
                        raise SpoolError("Event ID already contains different evidence")
                    return envelope["packet"]["event_id"]
            if len(records) >= self.max_records or size + self._charge(envelope) > self.max_bytes:
                raise SpoolError("Observation spool is full")
            self._publish(directory, name, data)
        return envelope["packet"]["event_id"]

    @staticmethod
    def _acknowledged(project_id, packet, response):
        if not isinstance(response, dict):
            return False
        observed = datetime.fromisoformat(packet["observed_at"]).astimezone(timezone.utc).isoformat()
        expected = {"project_id": project_id, "observed_at": observed,
                    "event_id": packet["event_id"], "source_sequence": packet["source_sequence"],
                    "identity": {key: packet[key] for key in IDENTITY},
                    "state": packet["state"], "evidence_refs": packet["evidence_refs"]}
        actual = {key: response.get(key) for key in expected}
        try:
            observed = datetime.fromisoformat(actual["observed_at"])
            if observed.tzinfo is None or observed.utcoffset() is None:
                return False
            actual["observed_at"] = observed.astimezone(timezone.utc).isoformat()
            return _encode(actual) == _encode(expected)
        except (ValueError, TypeError, OverflowError):
            return False

    def _remember(self, directory, name, envelope, status, status_code):
        # Persist only typed metadata. Exception strings and remote bodies can
        # contain credentials or unbounded untrusted content.
        if type(status_code) is not int or not 100 <= status_code <= 599:
            status_code = None
        envelope["last_outcome"] = {"status": status, "status_code": status_code}
        self._publish(directory, name, _encode(envelope))

    def replay(self, transport, *, limit=None):
        """Try at most limit packets once. Unknown/conflicting evidence remains.

        Transport accepts (project_id, packet), returns record_observation's event,
        and must enforce its own finite timeout. DomainError is returned intact;
        other transport exceptions remain visible to the caller as unknown.
        """
        if limit is None:
            limit = min(32, self.max_records)
        if type(limit) is not int or not 0 < limit <= self.max_records:
            raise SpoolError("Replay limit must be positive and within record bound")
        outcomes = []
        with self._locked() as directory:
            records, _ = self._records(directory)
            os.fsync(directory)  # Reaffirm publication after an interrupted directory sync.
            for name, _, envelope in records[:limit]:
                packet = envelope["packet"]
                try:
                    response = transport(envelope["project_id"], json.loads(_encode(packet)))
                except Exception as error:
                    status = "conflict" if isinstance(error, DomainError) and error.status_code == 409 else "unknown"
                    self._remember(directory, name, envelope, status,
                                   error.status_code if isinstance(error, DomainError) else None)
                    outcomes.append(ReplayOutcome(packet["event_id"], status, error))
                    continue
                if self._acknowledged(envelope["project_id"], packet, response):
                    os.unlink(name, dir_fd=directory)
                    os.fsync(directory)
                    outcomes.append(ReplayOutcome(packet["event_id"], "acknowledged", response))
                else:
                    status = "conflict" if isinstance(response, dict) and response.get("code") == "idempotency_conflict" else "unknown"
                    self._remember(directory, name, envelope, status, None)
                    outcomes.append(ReplayOutcome(packet["event_id"], status, response))
        return outcomes
