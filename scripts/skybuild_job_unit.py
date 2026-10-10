"""Launch and observe one already-admitted SkyBuild attempt in a systemd user unit.

The caller owns task admission and authority. This module only creates a durable
launch intent, applies local resource limits, and reads the resulting unit.
"""

from __future__ import annotations

import hashlib
import fcntl
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping
from uuid import uuid4


IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")
ENVIRONMENT = frozenset({"HOME", "PATH", "USER", "LOGNAME", "LANG", "LC_ALL", "TERM", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"})
MAX_RUNTIME_SECONDS = 8 * 60 * 60
Runner = Callable[..., subprocess.CompletedProcess[str]]


class JobUnitError(RuntimeError):
    """The attempt's unit or durable launch state is unsafe or unavailable."""


@dataclass(frozen=True)
class JobSpec:
    task_id: str
    attempt_id: str
    worktree: Path
    argv: tuple[str, ...]
    stdin_path: Path
    log_path: Path
    memory_high_bytes: int
    memory_max_bytes: int
    runtime_seconds: int
    environment: Mapping[str, str] = field(default_factory=dict)
    launch_nonce: str | None = None

    def unit(self) -> str:
        identity = json.dumps([self.task_id, self.attempt_id], separators=(",", ":"))
        return f"skybuild-job-{hashlib.sha256(identity.encode()).hexdigest()[:24]}.service"

    def validate(self) -> None:
        if not isinstance(self.task_id, str) or not isinstance(self.attempt_id, str) or not IDENTIFIER.fullmatch(self.task_id) or not IDENTIFIER.fullmatch(self.attempt_id):
            raise JobUnitError("task_id and attempt_id need safe stable identifiers")
        if not self.worktree.is_absolute() or not self.worktree.is_dir() or self.worktree.is_symlink() or "\n" in str(self.worktree) or "\r" in str(self.worktree):
            raise JobUnitError("worktree must be an existing absolute directory")
        if not self.argv or not Path(self.argv[0]).is_absolute() or not all(isinstance(arg, str) and "\x00" not in arg for arg in self.argv):
            raise JobUnitError("argv needs an absolute executable and valid arguments")
        for path, label in ((self.stdin_path, "stdin_path"), (self.log_path, "log_path")):
            if not path.is_absolute() or "\n" in str(path) or "\r" in str(path):
                raise JobUnitError(f"{label} must be an absolute path without line breaks")
        if not self.stdin_path.is_file() or self.stdin_path.is_symlink():
            raise JobUnitError("stdin_path must be an existing regular file")
        if not self.log_path.parent.is_dir() or self.log_path.is_symlink():
            raise JobUnitError("log_path parent must exist and log_path must not be a symlink")
        if type(self.memory_high_bytes) is not int or type(self.memory_max_bytes) is not int or not 0 < self.memory_high_bytes < self.memory_max_bytes:
            raise JobUnitError("MemoryHigh must be positive and below MemoryMax")
        if type(self.runtime_seconds) is not int or not 0 < self.runtime_seconds <= MAX_RUNTIME_SECONDS:
            raise JobUnitError("runtime must be between 1 second and 8 hours")
        if self.launch_nonce is not None and not re.fullmatch(r"[0-9a-f]{32}", self.launch_nonce):
            raise JobUnitError("launch_nonce must be a pinned 32-character lowercase hex value")
        if set(self.environment) - ENVIRONMENT or not all(isinstance(value, str) and "\x00" not in value for value in self.environment.values()):
            raise JobUnitError("environment contains unsupported keys or values")


@dataclass(frozen=True)
class JobUnitState:
    unit: str
    phase: str
    loaded: bool
    active: bool
    result: str | None
    exit_status: int | None
    control_group: str | None
    memory_current_bytes: int | None
    memory_peak_bytes: int | None
    memory_max_bytes: int
    invocation_id: str | None = None
    launch_nonce: str | None = None


def _counter(value: str | None) -> int | None:
    if value and value.isdecimal():
        number = int(value)
        if number < 2**63 - 1:
            return number
    return None


class JobUnitManager:
    def __init__(self, state_dir: Path, *, run: Runner = subprocess.run, timeout: float = 5,
                 systemctl: str = "systemctl", systemd_run: str = "systemd-run", env_bin: str = "/usr/bin/env") -> None:
        if not state_dir.is_absolute() or state_dir.is_symlink():
            raise JobUnitError("state_dir must be an absolute non-symlink directory")
        if not 0 < timeout <= 30:
            raise JobUnitError("systemd command timeout must be between 0 and 30 seconds")
        self.state_dir = state_dir
        self.run = run
        self.timeout = timeout
        self.systemctl = systemctl
        self.systemd_run = systemd_run
        self.env_bin = env_bin

    def _call(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return self.run(argv, capture_output=True, text=True, timeout=self.timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise JobUnitError(f"{argv[0]} did not return a usable result: {error}") from None

    def _manifest_path(self, unit: str) -> Path:
        if not re.fullmatch(r"skybuild-job-[0-9a-f]{24}\.service", unit):
            raise JobUnitError("unit name is not a SkyBuild attempt unit")
        return self.state_dir / f"{unit}.json"

    def _check_state_dir(self) -> None:
        if self.state_dir.is_symlink() or not self.state_dir.is_dir():
            raise JobUnitError("state_dir must remain a real directory")
        stat = self.state_dir.stat()
        if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
            raise JobUnitError("state_dir must be owned by this user and mode 0700")

    def _read_manifest(self, unit: str) -> dict:
        self._check_state_dir()
        path = self._manifest_path(unit)
        if path.is_symlink():
            raise JobUnitError("attempt manifest must not be a symlink")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise JobUnitError(f"attempt manifest unavailable: {error}") from None
        if not isinstance(data, dict) or data.get("unit") != unit:
            raise JobUnitError("attempt manifest has a different unit identity")
        return data

    def _show(self, unit: str) -> dict[str, str]:
        properties = ("LoadState", "ActiveState", "SubState", "Result", "ExecMainStatus", "ControlGroup", "MemoryCurrent", "MemoryPeak", "MemoryMax", "InvocationID", "Description")
        result = self._call([self.systemctl, "--user", "show", unit, *[item for prop in properties for item in ("-p", prop)]])
        if result.returncode != 0:
            raise JobUnitError(f"systemctl show failed for {unit}: {(result.stderr or result.stdout).strip()}")
        props = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if props.get("LoadState") not in {"loaded", "not-found"}:
            raise JobUnitError(f"systemctl show returned an unknown load state for {unit}")
        return props

    def _write_manifest(self, unit: str, manifest: dict) -> None:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.state_dir, delete=False) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            json.dump(manifest, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.replace(temporary, self._manifest_path(unit))
            directory = os.open(self.state_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _check_invocation(manifest: dict, props: dict) -> None:
        invocation = manifest.get("invocation_id")
        nonce = manifest.get("launch_nonce")
        if (not isinstance(invocation, str) or not re.fullmatch(r"[0-9a-f]{32}", invocation)
                or invocation == "0" * 32 or not isinstance(nonce, str)
                or not re.fullmatch(r"[0-9a-f]{32}", nonce)):
            raise JobUnitError("launch invocation is unpinned; reconcile the retained intent")
        if props.get("LoadState") == "loaded" and (
                props.get("InvocationID") != invocation
                or props.get("Description") != f"SkyBuild launch {nonce}"):
            raise JobUnitError("unit invocation differs from the pinned launch; reconcile replacement")

    def start(self, spec: JobSpec) -> str:
        """Create one launch intent and submit it once; never retry an uncertain start."""
        spec.validate()
        unit = spec.unit()
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._check_state_dir()
        if self._manifest_path(unit).exists():
            raise JobUnitError("launch intent already exists; reconcile it before any new attempt")
        if self._show(unit).get("LoadState") == "loaded":
            raise JobUnitError("unit already exists; reconcile it before any new attempt")
        manifest = {
            "unit": unit, "task_id": spec.task_id, "attempt_id": spec.attempt_id,
            "worktree": str(spec.worktree), "phase": "launch_intent",
            "memory_max_bytes": spec.memory_max_bytes, "memory_current_bytes": None,
            "memory_peak_bytes": None, "control_group": None, "observed_at": None,
            "launch_nonce": spec.launch_nonce or uuid4().hex, "invocation_id": None,
        }
        path = self._manifest_path(unit)
        try:
            with path.open("x", encoding="utf-8") as stream:
                os.chmod(path, 0o600)
                json.dump(manifest, stream, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            directory = os.open(self.state_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except FileExistsError:
            raise JobUnitError("launch intent already exists; reconcile it before any new attempt") from None
        properties = (
            f"Description=SkyBuild launch {manifest['launch_nonce']}",
            "Nice=10", f"MemoryHigh={spec.memory_high_bytes}", f"MemoryMax={spec.memory_max_bytes}",
            "MemorySwapMax=0", "OOMPolicy=continue", "NoNewPrivileges=yes",
            f"RuntimeMaxSec={spec.runtime_seconds}", f"WorkingDirectory={spec.worktree}",
            f"StandardInput=file:{spec.stdin_path}", f"StandardOutput=append:{spec.log_path}",
            f"StandardError=append:{spec.log_path}", "RemainAfterExit=yes",
        )
        command = [self.systemd_run, "--user", f"--unit={unit}", "--quiet", "--expand-environment=no"]
        for prop in properties:
            command.extend(("-p", prop))
        command.extend(("--", self.env_bin, "-i"))
        command.extend(f"{key}={value}" for key, value in sorted(spec.environment.items()))
        command.extend(spec.argv)
        result = self._call(command)
        if result.returncode != 0:
            raise JobUnitError(f"systemd-run failed for {unit}: {(result.stderr or result.stdout).strip()}; launch intent retained")
        props = self._show(unit)
        manifest["invocation_id"] = props.get("InvocationID")
        if props.get("LoadState") != "loaded":
            raise JobUnitError("launched unit is unavailable; launch intent retained")
        self._check_invocation(manifest, props)
        self._write_manifest(unit, manifest)
        return unit

    def observe(self, unit: str) -> JobUnitState:
        """Read a known unit; persist its latest metrics for later capacity analysis."""
        lock_path = self._manifest_path(unit).with_suffix(".lock")
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            manifest = self._read_manifest(unit)
            props = self._show(unit)
            self._check_invocation(manifest, props)
            loaded = props.get("LoadState") == "loaded"
            active_state = props.get("ActiveState")
            substate = props.get("SubState")
            ended = loaded and (active_state in {"inactive", "failed"} or (active_state == "active" and substate == "exited"))
            active = loaded and not ended and active_state in {"active", "activating", "reloading", "deactivating"}
            phase = "completed" if ended or (manifest.get("phase") == "completed" and not loaded) else "running" if active else "unknown"
            current = _counter(props.get("MemoryCurrent"))
            peak = _counter(props.get("MemoryPeak"))
            old_peak = manifest.get("memory_peak_bytes")
            if isinstance(old_peak, int) and old_peak >= 0:
                peak = max(old_peak, peak or 0)
            group = props.get("ControlGroup") or None
            if group and (not group.startswith("/") or ".." in Path(group).parts):
                group = None
            manifest.update(phase=phase, control_group=group or manifest.get("control_group"),
                            memory_current_bytes=current, memory_peak_bytes=peak,
                            observed_at=datetime.now(timezone.utc).isoformat())
            self._write_manifest(unit, manifest)
        return JobUnitState(unit, phase, loaded, active, props.get("Result") or None,
                            _counter(props.get("ExecMainStatus")), group, current, peak,
                            manifest["memory_max_bytes"], manifest.get("invocation_id"),
                            manifest.get("launch_nonce"))

    def stop(self, unit: str) -> None:
        """Refuse name-based stops until an invocation-bound effect is qualified."""
        self._read_manifest(unit)
        raise JobUnitError("stop is unavailable: systemctl stop cannot atomically guard the pinned invocation")
