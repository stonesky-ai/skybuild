#!/usr/bin/env python3
"""Control the exact existing SkyBuild services in an explicit host inventory."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import socket
import ssl
import subprocess
import sys
import time
import urllib.request
import urllib.error


class ServiceError(ValueError):
    pass


def command(argv, *, env=None):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=30, env=env)
    if result.returncode:
        raise ServiceError("Command failed; private driver output was suppressed")
    return result.stdout


def validate(item):
    if item.get("kind") not in {"container", "unit", "capability"}:
        raise ServiceError("Unknown service kind")
    if not re.fullmatch(r"skybuild[-a-z0-9_.]+", item.get("name", "")):
        raise ServiceError("Only explicit SkyBuild names are allowed")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", item.get("host", "")):
        raise ServiceError("Invalid host")
    if item["kind"] == "container":
        if not re.fullmatch(r"[0-9a-f]{64}", item.get("id", "")):
            raise ServiceError("An exact container ID is required")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", item.get("image", "")):
            raise ServiceError("An exact image ID is required")
        if type(item.get("memory_max_bytes")) is not int or item["memory_max_bytes"] <= 0:
            raise ServiceError("An exact positive memory limit is required")
        if item.get("project") != "skybuild-pilot" or not item.get("service"):
            raise ServiceError("Exact SkyBuild Compose labels are required")
    if item["kind"] == "unit":
        if not item["name"].endswith(".service") or not Path(item.get("fragment", "")).is_absolute():
            raise ServiceError("An exact user service fragment is required")
    return item


def readiness(item):
    if "readiness_url" not in item:
        return None
    url = item["readiness_url"]
    if not url.startswith("https://"):
        raise ServiceError("Readiness requires HTTPS")
    context = ssl.create_default_context(cafile=item["ca_file"])
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context))
    try:
        with opener.open(url, timeout=5) as response:
            return response.status == 200 and json.load(response) == {"status": "ready"}
    except (urllib.error.URLError, json.JSONDecodeError):
        return False


def check_watch(item):
    path = item.get("hostwatch_file")
    if not path:
        raise ServiceError("A fresh same-host watch is required before start")
    data = json.loads(Path(path).read_text())
    sampled = datetime.fromisoformat(data["sampled_at"].replace("Z", "+00:00"))
    age = (datetime.now(timezone.utc) - sampled).total_seconds()
    if data.get("status") != "ok" or not 0 <= age <= 90:
        raise ServiceError("Host watch is stale or blocked")
    if data.get("available_bytes", 0) - item["memory_max_bytes"] < 8 * 1024**3:
        raise ServiceError("Host watch reserve blocks start")


def local(item, action):
    validate(item)
    if socket.gethostname().lower() != item["host"].lower():
        raise ServiceError("Execution host does not match inventory")
    if item["kind"] == "capability":
        if action == "stop":
            return {"state": "capability", "ok": True, "managed": False, "skipped": True}
        checkout = Path(item["checkout"])
        missing = [f for f in item.get("required_files", []) if not (checkout / f).is_file()]
        head = command(["git", "-C", str(checkout), "rev-parse", "HEAD"]).strip() if checkout.is_dir() else None
        available = not missing and head == item["head"]
        return {"state": "available" if available else "missing", "active_job": False,
                "ok": available, "managed": False}
    env = dict(os.environ)
    if item["kind"] == "unit":
        uid = os.getuid()
        runtime = Path("/run/user") / str(uid)
        if not (runtime / "bus").exists():
            raise ServiceError("User bus is unavailable")
        env.update(XDG_RUNTIME_DIR=str(runtime), DBUS_SESSION_BUS_ADDRESS="unix:path=" + str(runtime / "bus"))
        def unit(*args):
            return command(["systemctl", "--user", *args], env=env)
        data = dict(line.split("=", 1) for line in unit("show", item["name"], "--property=Id,FragmentPath,Transient,ActiveState,SubState").splitlines())
        if data.get("Id") != item["name"] or data.get("FragmentPath") != item["fragment"]:
            raise ServiceError("User service identity does not match")
        running = data.get("ActiveState") == "active" and (data.get("SubState") == "running" or (item.get("oneshot") is True and data.get("SubState") == "exited"))
        if item.get("observation_only") is True or data.get("Transient") == "yes":
            return {"state": data.get("SubState", "unknown"), "ok": True if action == "stop" else running, "managed": False, "skipped": action != "status", "observation_only": True}
        if action != "status":
            if action == "stop":
                unit("stop", item["name"])
            elif not running:
                unit("start", item["name"])
            result = local(item, "status")
            result["desired_state"] = "stopped" if action == "stop" else "running"
            if action == "stop":
                result["ok"] = not running_unit(item, env)
            return result
        return {"state": data.get("SubState", "unknown"), "ok": running, "managed": True}
    daemon = command(["docker", "info", "--format", "{{.Name}}"]).strip()
    if daemon.lower() != item["host"].lower():
        raise ServiceError("Docker daemon host does not match")
    rows = json.loads(command(["docker", "inspect", item["name"]]))
    if len(rows) != 1:
        raise ServiceError("Container identity is ambiguous")
    row = rows[0]
    labels = row["Config"].get("Labels") or {}
    if (row["Id"] != item["id"] or row["Image"] != item["image"]
            or row["Name"] != "/" + item["name"]
            or labels.get("com.docker.compose.project") != item["project"]
            or labels.get("com.docker.compose.service") != item["service"]):
        raise ServiceError("Container identity does not match")
    if row["HostConfig"].get("Memory") != item["memory_max_bytes"]:
        raise ServiceError("Container memory limit does not match")
    running = row["State"]["Running"]
    if action == "stop":
        if running:
            command(["docker", "stop", "--time", "10", item["id"]])
            result = local(item, "status")
        else:
            result = {"state": "stopped", "ready": False, "health": None, "managed": True}
        result.update(desired_state="stopped", ok=result["state"] == "stopped")
        return result
    if action in {"start", "ensure-running"} and not running:
        check_watch(item)
        available = int(next(x for x in Path("/proc/meminfo").read_text().splitlines() if x.startswith("MemAvailable:")).split()[1]) * 1024
        if available - item["memory_max_bytes"] < 8 * 1024**3:
            raise ServiceError("Host memory reserve blocks start")
        command(["docker", "start", item["id"]])
        deadline = time.monotonic() + 20
        while True:
            result = local(item, "status")
            result["desired_state"] = "running"
            if result["ok"] or time.monotonic() >= deadline:
                return result
            time.sleep(1)
    ready = readiness(item) if running else False
    healthy = row["State"].get("Health", {}).get("Status")
    ok = running and healthy not in {"starting", "unhealthy"} and ready is not False
    return {"state": "running" if running else "stopped", "ready": ready, "health": healthy, "ok": ok, "managed": True}


def running_unit(item, env):
    state = command(["systemctl", "--user", "show", item["name"], "--property=ActiveState", "--value"], env=env).strip()
    return state in {"active", "activating", "deactivating", "reloading"}


def operate(item, action, script):
    if socket.gethostname().lower() == item["host"].lower():
        return local(item, action)
    argv = ["python3", "-", "--local", action]
    payload = '__name__ = "_skybuild_service_host"\n' + Path(script).read_text() + "\n"
    # Data travels on stdin. The remote shell receives only fixed arguments.
    payload += "\ntry:\n print(json.dumps(local(" + repr(item) + ", " + repr(action) + ")))\nexcept Exception:\n print(json.dumps({'state':'error','ok':False,'error':'Host service check failed'}))\n"
    result = subprocess.run(["ssh", "-F", str(Path.home() / ".ssh/config"), "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", item["host"], shlex.join(argv)], input=payload, text=True, capture_output=True, timeout=90)
    if result.returncode:
        raise ServiceError("Host is unavailable; private SSH output was suppressed")
    return json.loads(result.stdout)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", default="status", choices=["status", "start", "stop", "ensure-running"])
    parser.add_argument("--inventory", type=Path, default=Path(__file__).resolve().parent.parent / "ops/services/inventory.json")
    parser.add_argument("--host", action="append", choices=["jeltz", "wonko", "wowbagger"])
    args = parser.parse_args(argv)
    data = json.loads(args.inventory.read_text())
    if data.get("schema") != "skybuild.services.v1":
        raise ServiceError("Unknown inventory schema")
    items = [validate(x) for x in data["services"]]
    if len({(x["host"], x["name"]) for x in items}) != len(items):
        raise ServiceError("Duplicate host service")
    if args.action == "stop":
        items.reverse()
    results = []
    for item in items:
        if args.host and item["host"] not in args.host:
            continue
        try:
            if item.get("blocked"):
                raise ServiceError("A prerequisite service on this host failed")
            result = operate(item, args.action, __file__)
        except Exception as error:
            result = {"state": "error", "ok": False, "error": str(error) if isinstance(error, ServiceError) else "Service check failed"}
        results.append({"host": item["host"], "name": item["name"], **result})
        # Do not start dependent services on this host after a failed start.
        if args.action in {"start", "ensure-running"} and not result["ok"]:
            for later in items[items.index(item)+1:]:
                if later["host"] == item["host"]:
                    later["blocked"] = True
    print(json.dumps({"action": args.action, "services": results}, sort_keys=True))
    return 0 if results and all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
