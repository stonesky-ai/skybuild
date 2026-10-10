"""Test the real service lifecycle with a detached Node child on the named host.

This test does not launch a model, use credentials, or change a Git ref.
It starts short-lived services and sends SIGKILL only through a pinned pidfd.
"""

import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time


def identity(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return {"pid": pid, "start_ticks": fields[19], "state": fields[0]}
    except FileNotFoundError:
        return None


def payload(args):
    if args.target_ref != "refs/heads/dev-006":
        raise ValueError("The qualification target differs from the approved target")
    child = subprocess.Popen(["/usr/bin/node", "-e", "setInterval(() => {}, 1000)"], start_new_session=True)
    pending = args.output / "processes.pending"
    pending.write_text(json.dumps({"unit": os.environ["SKYBUILD_SESSION_UNIT"],
                                   "main": identity(os.getpid()), "child": identity(child.pid)}))
    pending.replace(args.output / "processes.json")
    while not (args.output / "finish").exists():
        time.sleep(0.05)
    if args.case == "oom":
        memory = bytearray(3 * 1024**3)
        for offset in range(0, len(memory), 4096):
            memory[offset] = 1
        time.sleep(60)
    return 0


def qualify(args):
    if socket.gethostname().casefold() != args.expected_host.casefold():
        raise ValueError("The qualification host differs from the required host")
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    runner = Path(__file__).with_name("managed_session.py")
    reports = []
    for case in ("normal", "sigkill", "timeout", "oom"):
        case_dir = args.output / case
        case_dir.mkdir(mode=0o700)
        command = [sys.executable, str(runner), "--profile", "merge", "--expected-host", args.expected_host,
                   "--checkout", str(args.checkout), "--target-ref", args.target_ref,
                   "--memory-high-gib", "1.9999" if case == "oom" else "1", "--memory-max-gib", "2",
                   "--runtime-seconds", "8" if case == "timeout" else "45", "--",
                   sys.executable, str(Path(__file__).resolve()), "--payload", "--case", case,
                   "--output", str(case_dir), "--target-ref", args.target_ref]
        with (case_dir / "service.log").open("w") as log:
            process = subprocess.Popen(command, stdout=log, stderr=log)
            started = time.monotonic()
            receipt = case_dir / "processes.json"
            while not receipt.exists():
                if process.poll() is not None or time.monotonic() - started > 20:
                    raise RuntimeError(f"The {case} service did not publish its process identities")
                time.sleep(0.05)
            processes = json.loads(receipt.read_text())
            if case == "sigkill":
                main = processes["main"]
                descriptor = os.pidfd_open(main["pid"])
                try:
                    if identity(main["pid"])["start_ticks"] != main["start_ticks"]:
                        raise RuntimeError("The main process identity changed")
                    signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                finally:
                    os.close(descriptor)
            elif case in {"normal", "oom"}:
                (case_dir / "finish").touch()
            status = process.wait(timeout=65)
        for name in ("main", "child"):
            recorded = processes[name]
            actual = identity(recorded["pid"])
            if actual and actual["start_ticks"] == recorded["start_ticks"] and actual["state"] != "Z":
                raise RuntimeError(f"The {case} service left its {name} process alive")
        if (case == "normal") != (status == 0):
            raise RuntimeError(f"The {case} service returned an unexpected exit status")
        state = Path.home() / "my_code/skybuild-managed-state"
        evidence = json.loads((state / f"{processes['unit']}.result.json").read_text())
        expected = {"normal": "success", "sigkill": "signal", "timeout": "timeout", "oom": "oom-kill"}[case]
        if evidence.get("systemd_result") != expected or evidence.get("cleanup_confirmed") is not True:
            raise RuntimeError(f"The {case} service has no matching result and cleanup evidence")
        if case == "oom" and int((evidence.get("memory_events") or {}).get("oom_kill", 0)) < 1:
            raise RuntimeError("The OOM service has no cgroup OOM kill event")
        if (state / "merge-slot.json").exists():
            raise RuntimeError(f"The {case} service did not release its merge slot")
        report = {"case": case, "exit_code": status, "descendants_stopped": True,
                  "merge_slot_released": True, "systemd_result": evidence["systemd_result"],
                  "memory_peak_bytes": evidence.get("memory_peak_bytes")}
        reports.append(report)
        print(json.dumps(report), flush=True)
    (args.output / "summary.json").write_text(json.dumps({"host": socket.gethostname(), "cases": reports}, indent=2)+"\n")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload", action="store_true")
    parser.add_argument("--case")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-host", default="wonko")
    parser.add_argument("--checkout", type=Path)
    parser.add_argument("--target-ref", default="refs/heads/dev-006")
    args = parser.parse_args()
    raise SystemExit(payload(args) if args.payload else qualify(args))
