"""Read-only Tailscale worker inventory for manual Workbench refreshes."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
import json
import math
import re
import subprocess
import sys
import threading
import time
from typing import Any

MIN_REFRESH_SECONDS = 10.0
TAILSCALE_TIMEOUT_SECONDS = 5.0
BOX_TIMEOUT_SECONDS = 9.0
MAX_PARALLEL_BOX_QUERIES = 8

_PROBE = r'''import json, os, platform, re, shutil, subprocess, time
def meminfo():
    values = {}
    try:
        for line in open("/proc/meminfo", encoding="ascii"):
            match = re.match(r"^(MemTotal|MemAvailable|SwapTotal|SwapFree):\s+(\d+)\s+kB", line)
            if match:
                values[match.group(1)] = int(match.group(2)) * 1024
    except OSError:
        pass
    return values
def cpu_model():
    try:
        text = open("/proc/cpuinfo", encoding="utf-8", errors="replace").read()
        match = re.search(r"^(?:model name|Hardware|Processor)\s*:\s*(.+)$", text, re.M)
        return match.group(1).strip() if match else (platform.processor() or None)
    except OSError:
        return platform.processor() or None
def physical_cores():
    try:
        text = open("/proc/cpuinfo", encoding="ascii", errors="replace").read()
        pairs = set()
        for block in text.split("\n\n"):
            package = re.search(r"^physical id\s*:\s*(\d+)$", block, re.M)
            core = re.search(r"^core id\s*:\s*(\d+)$", block, re.M)
            if package and core:
                pairs.add((package.group(1), core.group(1)))
        return len(pairs) or None
    except OSError:
        return None
def os_release():
    try:
        data = platform.freedesktop_os_release()
        return {"name": data.get("PRETTY_NAME") or data.get("NAME"),
                "version": data.get("VERSION_ID") or data.get("VERSION")}
    except (AttributeError, OSError):
        return {"name": platform.system() or None, "version": platform.version() or None}
try:
    disk = shutil.disk_usage(os.path.abspath(os.sep))
    disk_data = {"total_bytes": disk.total, "free_bytes": disk.free}
except OSError:
    disk_data = {"total_bytes": None, "free_bytes": None}
try:
    load = list(os.getloadavg())
except (AttributeError, OSError):
    load = None
try:
    uptime = float(open("/proc/uptime", encoding="ascii").read().split()[0])
except (OSError, ValueError, IndexError):
    uptime = None
gpus = []
try:
    result = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,memory.free",
        "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=2, check=False)
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            parts = [item.strip() for item in line.split(",", 2)]
            if len(parts) == 3:
                gpus.append({"name": parts[0], "memory_total_mib": parts[1], "memory_free_mib": parts[2]})
except (OSError, subprocess.TimeoutExpired):
    pass
print(json.dumps({"hostname": platform.node(), "os_name": os_release()["name"],
    "os_version": os_release()["version"], "kernel": platform.release(),
    "architecture": platform.machine(), "cpu_model": cpu_model(),
    "logical_cores": os.cpu_count(), "physical_cores": physical_cores(),
    "memory": meminfo(), "root_disk": disk_data, "load_average": load,
    "uptime_seconds": uptime, "python_version": platform.python_version(), "gpus": gpus}))
'''

_lock = threading.Lock()
_last_started = 0.0
_in_flight = False
_payload: dict[str, Any] = {"nodes": [], "refreshed_at": None, "error": ""}
_last_stats: dict[str, dict[str, Any]] = {}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def snapshot() -> dict[str, Any]:
    with _lock:
        result = dict(_payload)
        result["nodes"] = [dict(node) for node in _payload.get("nodes", [])]
        result["refreshing"] = _in_flight
        result["retry_after"] = max(0, math.ceil(MIN_REFRESH_SECONDS - (time.monotonic() - _last_started))) if _last_started else 0
        return result


def _box_stats(node: dict[str, Any]) -> dict[str, Any]:
    if node.get("self"):
        done = subprocess.run([sys.executable, "-c", _PROBE], capture_output=True, text=True,
                              timeout=BOX_TIMEOUT_SECONDS, check=False)
    else:
        dns_name = str(node.get("dns_name") or "").rstrip(".")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,250}", dns_name):
            raise RuntimeError("tailnet DNS name is not usable for SSH")
        done = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=yes",
             dns_name, "python3", "-"], input=_PROBE, capture_output=True, text=True,
            timeout=BOX_TIMEOUT_SECONDS, check=False,
        )
    if done.returncode:
        tail = (done.stderr or "box query failed").strip().splitlines()[-1]
        raise RuntimeError(tail[:240])
    data = json.loads(done.stdout)
    if not isinstance(data, dict):
        raise RuntimeError("box query returned invalid data")
    return data


def refresh() -> tuple[int, dict[str, Any]]:
    """Refresh once per ten seconds; each online tailnet peer gets one SSH probe."""
    global _last_started, _in_flight, _payload
    now = time.monotonic()
    with _lock:
        wait = max(0, math.ceil(MIN_REFRESH_SECONDS - (now - _last_started))) if _last_started else 0
        if _in_flight or wait:
            result = dict(_payload)
            result["nodes"] = [dict(node) for node in _payload.get("nodes", [])]
            result["refreshing"] = _in_flight
            result["retry_after"] = max(1, wait, 2 if _in_flight else 0)
            result["error"] = "Refresh rate limit: wait before querying the tailnet again."
            return 429, result
        _last_started = now
        _in_flight = True

    try:
        status = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True,
                                timeout=TAILSCALE_TIMEOUT_SECONDS, check=False)
        if status.returncode:
            raise RuntimeError((status.stderr or "tailscale status failed").strip()[:240])
        raw = json.loads(status.stdout)
        self_node = raw.get("Self") if isinstance(raw, dict) else None
        peers = raw.get("Peer") if isinstance(raw, dict) else None
        users = raw.get("User") if isinstance(raw, dict) else None
        if not isinstance(self_node, dict) or not isinstance(peers, dict):
            raise RuntimeError("tailscale status returned an incomplete device list")
        if not isinstance(users, dict):
            users = {}
        raw_nodes = [dict(self_node, Self=True), *[value for value in peers.values() if isinstance(value, dict)]]
        nodes = []
        for item in raw_nodes:
            dns = str(item.get("DNSName") or "").rstrip(".")
            addresses = item.get("TailscaleIPs") if isinstance(item.get("TailscaleIPs"), list) else []
            node_id = str(item.get("ID") or dns or item.get("HostName") or "unknown")
            user = users.get(str(item.get("UserID")), {})
            if not isinstance(user, dict):
                user = {}
            nodes.append({
                "id": node_id, "name": str(item.get("HostName") or dns or "Unnamed device"),
                "dns_name": dns or None, "ips": [str(address) for address in addresses],
                "online": bool(item.get("Online", False)), "self": bool(item.get("Self")),
                "tailnet_os": str(item.get("OS") or "unknown"),
                "tailnet_user": user.get("DisplayName") or user.get("LoginName"), "user_id": item.get("UserID"),
                "last_seen": item.get("LastSeen") or None,
                "created": item.get("Created") or None,
                "active": bool(item.get("Active", False)),
            })

        eligible = [node for node in nodes if node["online"]]
        results: dict[str, tuple[dict[str, Any] | None, str]] = {}
        workers = min(MAX_PARALLEL_BOX_QUERIES, len(eligible))
        if workers:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="fleet-query") as pool:
                pending = {pool.submit(_box_stats, node): node for node in eligible}
                for future in as_completed(pending):
                    node = pending[future]
                    try:
                        results[node["id"]] = (future.result(), "")
                    except Exception as error:  # each box failure stays local to its row
                        results[node["id"]] = (None, f"{type(error).__name__}: {str(error)[:220]}")

        for node in nodes:
            current, problem = results.get(node["id"], (None, ""))
            if current is not None:
                _last_stats[node["id"]] = {"stats": current, "stats_at": _utc_now()}
            previous = _last_stats.get(node["id"], {})
            node["stats"] = current or previous.get("stats")
            node["stats_at"] = _utc_now() if current is not None else previous.get("stats_at")
            node["query_error"] = problem
            node["status"] = "online" if node["online"] else "offline"
        nodes.sort(key=lambda node: (not node["online"], node["name"].lower()))
        payload = {"nodes": nodes, "refreshed_at": _utc_now(), "error": "", "refreshing": False,
                   "retry_after": 0}
        with _lock:
            _payload = payload
        return 200, payload
    except (OSError, subprocess.TimeoutExpired, ValueError, RuntimeError) as error:
        with _lock:
            _payload = {**_payload, "error": f"Tailnet inventory failed: {type(error).__name__}: {str(error)[:240]}"}
            result = dict(_payload)
            result["refreshing"] = False
            result["retry_after"] = 0
        return 503, result
    finally:
        with _lock:
            _in_flight = False
