"""Record service cleanup after systemd stops the main command and its children.

systemd runs this file as ExecStopPost. The file uses only the standard library.
It records no command output, environment secrets, or credentials.
"""

import json
import os
from pathlib import Path
import sys
import time


def record(path):
    if path.is_symlink() or path.stat().st_size > 16384:
        raise ValueError("The launch record is unsafe")
    launch = json.loads(path.read_text())
    group = next(line.split(":", 2)[2] for line in Path("/proc/self/cgroup").read_text().splitlines()
                 if line.startswith("0::"))
    if not group.startswith("/") or ".." in Path(group).parts:
        raise ValueError("The control group path is invalid")
    group_path = Path("/sys/fs/cgroup") / group.lstrip("/")
    remaining = set()
    for counter in group_path.rglob("cgroup.procs"):
        remaining.update(int(pid) for pid in counter.read_text().split())
    remaining.discard(os.getpid())
    peak = group_path / "memory.peak"
    events = group_path / "memory.events"
    value = {"unit": launch["unit"], "cleanup_confirmed": not remaining,
             "remaining_process_count": len(remaining), "control_group": group,
             "memory_peak_bytes": int(peak.read_text()) if peak.is_file() else None,
             "memory_events": dict(line.split() for line in events.read_text().splitlines()) if events.is_file() else None,
             "systemd_result": os.environ.get("SERVICE_RESULT"),
             "exit_code": os.environ.get("EXIT_CODE"), "exit_status": os.environ.get("EXIT_STATUS"),
             "invocation_id": os.environ.get("INVOCATION_ID"), "finished_at": time.time()}
    destination = path.with_name(f"{launch['unit']}.result.json")
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    return 0 if not remaining else 1


if __name__ == "__main__":
    raise SystemExit(record(Path(sys.argv[1])))
