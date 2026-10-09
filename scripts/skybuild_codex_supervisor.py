#!/usr/bin/env python3
"""Cron-safe supervisor for one explicitly prepared, bounded Codex session.

This is a local recovery aid, not the future SkyBuild task admission service.
It never selects work, grants inference authority, or starts Docker.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time
import uuid

MAX_LOG_BYTES = 1024 * 1024
MAX_REQUEST_SECONDS = 8 * 60 * 60
MIN_AVAILABLE_GIB = 10
MEMORY_FLOOR_GIB = 8
TERMINAL_EVENTS = {"turn.completed", "turn.failed"}


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_deadline(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("deadline must include a UTC offset")
    return parsed.astimezone(dt.timezone.utc)


def available_gib() -> float:
    for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / (1024 * 1024)
    raise RuntimeError("MemAvailable unavailable")


def process_identity(pid: int) -> tuple[str, int] | None:
    """Return command and kernel start ticks; PID alone can be reused."""
    try:
        command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        fields = stat.rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        start_ticks = int(fields[19])
    except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError, IndexError):
        return None
    return command, start_ticks


def same_child(state: dict) -> bool:
    pid = state.get("pid")
    identity = process_identity(pid) if isinstance(pid, int) else None
    return bool(identity and identity[1] == state.get("start_ticks") and "codex" in identity[0])


def competing_codex(checkout: Path, own_pid: int | None = None) -> list[int]:
    """Conservatively detect live Codex sessions targeting this checkout."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        pid = int(entry.name)
        if pid == own_pid or pid == os.getpid():
            continue
        identity = process_identity(pid)
        if not identity:
            continue
        command = identity[0]
        words = command.split()
        if not any(Path(word).name == "codex" for word in words[:2]):
            continue
        try:
            cwd = Path(f"/proc/{pid}/cwd").resolve()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            cwd = None
        if cwd == checkout or str(checkout) in words:
            found.append(pid)
    return found


def append_log(path: Path, line: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "ab") as handle:
        handle.write(line[:65536])
    if path.stat().st_size > MAX_LOG_BYTES:
        with path.open("rb") as handle:
            handle.seek(-MAX_LOG_BYTES, os.SEEK_END)
            tail = handle.read()
        with path.open("wb") as handle:
            handle.write(tail)


def record_event(state: dict, line: bytes) -> None:
    try:
        event = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return
    if not isinstance(event, dict):
        return
    if event.get("type") == "thread.started":
        thread_id = event.get("thread_id")
        if isinstance(thread_id, str):
            try:
                uuid.UUID(thread_id)
            except ValueError:
                return
            state["session_id"] = thread_id
    if event.get("type") in TERMINAL_EVENTS:
        state["terminal_event"] = event["type"]


def bounded_timeout(state: dict) -> bool:
    return utc_now() >= parse_deadline(state["deadline_utc"])


def stop_owned_child(state: dict) -> None:
    if not same_child(state):
        return
    pid = state["pid"]
    # Popen(start_new_session=True) makes this child's PID its private group ID.
    if os.getpgid(pid) != pid:
        return
    os.killpg(pid, signal.SIGTERM)
    for _ in range(20):
        if not same_child(state):
            return
        time.sleep(0.1)
    if same_child(state) and os.getpgid(pid) == pid:
        os.killpg(pid, signal.SIGKILL)


def prepare(args: argparse.Namespace, state_file: Path) -> str:
    if (state_file.parent / "STOP").exists():
        raise ValueError("STOP file is present; remove it deliberately before preparing a new request")
    if state_file.exists():
        previous = load_json(state_file)
        if previous.get("phase") not in {"completed", "failed", "expired", "parked", "stopped", "memory_stop"}:
            raise ValueError("existing request is active; do not overwrite it")
    checkout = args.checkout.resolve(strict=True)
    if not (checkout / ".git").exists():
        raise ValueError("checkout must be a Git worktree")
    deadline = parse_deadline(args.deadline_utc)
    seconds = (deadline - utc_now()).total_seconds()
    if not 0 < seconds <= MAX_REQUEST_SECONDS:
        raise ValueError("deadline must be in the next eight hours")
    prompt = args.prompt_file.read_bytes()
    resume = args.resume_file.read_bytes()
    if not prompt.strip() or not resume.strip():
        raise ValueError("both prompts must contain text")
    for name, content in (("prompt.txt", prompt), ("resume.txt", resume)):
        target = state_file.parent / name
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    state = {
        "request_id": args.request_id,
        "checkout": str(checkout),
        "deadline_utc": deadline.isoformat(),
        "phase": "prepared",
        "runs": 0,
        "prompt_sha256": hashlib.sha256(prompt).hexdigest(),
        "resume_sha256": hashlib.sha256(resume).hexdigest(),
        "session_id": None,
        "terminal_event": None,
        "reversible_local_only": args.reversible_local_only,
    }
    atomic_json(state_file, state)
    return "prepared"


def inspect_previous(state: dict, stop_file: Path) -> str | None:
    if state["phase"] != "running":
        return None
    if same_child(state):
        if stop_file.exists():
            stop_owned_child(state)
            state["phase"] = "stopped"
            return "stopped"
        if bounded_timeout(state):
            stop_owned_child(state)
            state["phase"] = "expired"
            return "expired"
        if available_gib() < MEMORY_FLOOR_GIB:
            stop_owned_child(state)
            state["phase"] = "memory_stop"
            return "memory_stop"
        return "running"
    if state.get("terminal_event") == "turn.completed":
        state["phase"] = "completed"
        return "completed"
    if state.get("terminal_event") == "turn.failed":
        state["phase"] = "failed"
        return "failed"
    state["phase"] = "parked"
    state["reason"] = "supervisor did not observe child exit; effects are uncertain"
    return "parked"


def run_child(state: dict, state_file: Path, codex: Path) -> str:
    first_run = state["runs"] == 0
    if not first_run and (state["runs"] >= 2 or not state.get("session_id")):
        state["phase"] = "parked"
        state["reason"] = "resume limit reached or session ID missing"
        atomic_json(state_file, state)
        return "parked"
    checkout = Path(state["checkout"])
    prompt_path = state_file.parent / ("prompt.txt" if first_run else "resume.txt")
    expected_hash = state["prompt_sha256" if first_run else "resume_sha256"]
    prompt = prompt_path.read_bytes()
    if hashlib.sha256(prompt).hexdigest() != expected_hash:
        state["phase"] = "parked"
        state["reason"] = "prepared prompt changed"
        atomic_json(state_file, state)
        return "parked"
    if first_run:
        command = [str(codex), "exec", "--json", "--sandbox", "workspace-write", "--cd", str(checkout), "-"]
    else:
        command = [str(codex), "exec", "resume", "--json", state["session_id"], "-"]
    # GNU timeout remains alive if the cron supervisor dies. It bounds the
    # model process independently of the next cron tick.
    remaining = max(1, int((parse_deadline(state["deadline_utc"]) - utc_now()).total_seconds()))
    command = ["timeout", "--signal=TERM", "--kill-after=5", str(remaining)] + command
    state["phase"] = "launching"
    atomic_json(state_file, state)
    process = subprocess.Popen(
        command, cwd=checkout, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, start_new_session=True, bufsize=0,
    )
    identity = process_identity(process.pid)
    if identity is None:
        process.terminate()
        raise RuntimeError("cannot identify launched child")
    state.update(phase="running", runs=state["runs"] + 1, pid=process.pid,
                 start_ticks=identity[1], terminal_event=None)
    atomic_json(state_file, state)
    assert process.stdin is not None and process.stdout is not None
    process.stdin.write(prompt)
    process.stdin.close()
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    pending = bytearray()
    log = state_file.parent / "codex.jsonl"
    while True:
        if bounded_timeout(state) or (state_file.parent / "STOP").exists() or available_gib() < MEMORY_FLOOR_GIB:
            stop_owned_child(state)
            state["phase"] = (
                "expired" if bounded_timeout(state)
                else "stopped" if (state_file.parent / "STOP").exists()
                else "memory_stop"
            )
            break
        for key, _ in selector.select(timeout=1):
            chunk = os.read(key.fd, 65536)
            if not chunk:
                selector.unregister(process.stdout)
                continue
            pending.extend(chunk)
            if len(pending) > 128 * 1024:
                append_log(log, pending[:65536])
                pending.clear()
                state["output_truncated"] = True
                atomic_json(state_file, state)
                continue
            while b"\n" in pending:
                line, _, remainder = pending.partition(b"\n")
                pending = bytearray(remainder)
                append_log(log, line + b"\n")
                old_session = state.get("session_id")
                record_event(state, line)
                if state.get("session_id") != old_session or state.get("terminal_event"):
                    atomic_json(state_file, state)
        if process.poll() is not None and not selector.get_map():
            if pending:
                append_log(log, bytes(pending))
                record_event(state, pending)
            if process.returncode == 0 and state.get("terminal_event") == "turn.completed":
                state["phase"] = "completed"
            elif state.get("terminal_event") == "turn.failed":
                state["phase"] = "failed"
            else:
                state["phase"] = "parked"
                state["reason"] = "child exited without a completed or failed turn event"
            break
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        stop_owned_child(state)
        process.wait(timeout=5)
    state["exit_code"] = process.returncode
    atomic_json(state_file, state)
    if (state["phase"] == "parked" and process.returncode != 0
            and state.get("terminal_event") is None and state.get("session_id")
            and state.get("reversible_local_only") and state["runs"] == 1
            and (parse_deadline(state["deadline_utc"]) - utc_now()).total_seconds() >= 60
            and not (state_file.parent / "STOP").exists()
            and available_gib() >= MIN_AVAILABLE_GIB
            and not competing_codex(checkout)):
        # Only this still-running supervisor observed the failed child exit.
        # A later cron tick must never infer this transition from saved state.
        state["phase"] = "resume_pending"
        atomic_json(state_file, state)
        return run_child(state, state_file, codex)
    return state["phase"]


def tick(state_file: Path, codex: Path) -> str:
    if not state_file.exists():
        return "idle: no prepared request"
    state = load_json(state_file)
    if state["phase"] in {"completed", "failed", "expired", "parked", "stopped", "memory_stop"}:
        return state["phase"]
    if state["phase"] in {"resume_pending", "launching"}:
        state["phase"] = "parked"
        state["reason"] = "supervisor died during a launch; process start is uncertain"
        atomic_json(state_file, state)
        return "parked"
    result = inspect_previous(state, state_file.parent / "STOP")
    if result:
        atomic_json(state_file, state)
        return result
    if bounded_timeout(state):
        state["phase"] = "expired"
        atomic_json(state_file, state)
        return "expired"
    if (state_file.parent / "STOP").exists():
        state["phase"] = "stopped"
        atomic_json(state_file, state)
        return "stopped"
    if available_gib() < MIN_AVAILABLE_GIB:
        return "waiting: less than 10 GiB available"
    competitors = competing_codex(Path(state["checkout"]))
    if competitors:
        return "waiting: live Codex session in checkout"
    return run_child(state, state_file, codex)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "tick", "status", "probe"))
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--checkout", type=Path)
    parser.add_argument("--prompt-file", type=Path)
    parser.add_argument("--resume-file", type=Path)
    parser.add_argument("--deadline-utc")
    parser.add_argument("--request-id")
    parser.add_argument("--reversible-local-only", action="store_true")
    parser.add_argument("--codex", type=Path, default=Path("/home/kevin/.local/bin/codex"))
    args = parser.parse_args()
    state_dir = args.state_dir.resolve()
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if state_dir.stat().st_mode & 0o077:
        raise SystemExit("state directory must not be accessible to group or others")
    state_file = state_dir / "request.json"
    with (state_dir / "supervisor.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("running: another supervisor owns lock")
            return 0
        if args.command == "status":
            print(json.dumps(load_json(state_file) if state_file.exists() else {"phase": "idle"}, sort_keys=True))
            return 0
        if args.command == "probe":
            if args.checkout:
                checkout = args.checkout.resolve(strict=True)
            elif state_file.exists():
                checkout = Path(load_json(state_file)["checkout"])
            else:
                parser.error("probe needs --checkout or a prepared request")
            print(json.dumps({"checkout": str(checkout), "codex_pids": competing_codex(checkout),
                              "available_gib": round(available_gib(), 2)}, sort_keys=True))
            return 0
        if args.command == "prepare":
            if not all((args.checkout, args.prompt_file, args.resume_file, args.deadline_utc, args.request_id)):
                parser.error("prepare needs checkout, both prompt files, deadline and request ID")
            result = prepare(args, state_file)
        else:
            result = tick(state_file, args.codex)
        print(result)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, RuntimeError, json.JSONDecodeError) as error:
        print(f"supervisor error: {error}", file=sys.stderr)
        sys.exit(1)
