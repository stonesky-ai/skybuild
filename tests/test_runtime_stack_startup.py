"""Exercise startup ordering and refusal without starting real containers."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
DATABASE_ID = "072a77f1cc5e35a07296ab60fe9c9962ee358285f5dad7bbcc8dff966c0a0fcf"


@pytest.fixture
def startup(tmp_path):
    project = tmp_path / "stack checkout"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    script = scripts / "start_runtime_stack"
    script.write_text((ROOT / "scripts/start_runtime_stack").read_text())
    runner = scripts / "project_python"
    runner.write_text('#!/bin/sh\nexec "' + sys.executable + '" "$@"\n')
    runner.chmod(0o755)
    state = tmp_path / "pilot-state"
    (state / "pgdata").mkdir(parents=True)
    (state / "secrets").mkdir()
    (state / "secrets/admin-password").write_text("never-print-this-password")
    container = {
        "Id": DATABASE_ID,
        "Image": "sha256:1a6ab3f5345eb6dbe04a1349529caabdb0ab09293a09590fad07b2246bfa4b54",
        "Config": {"Image": "postgres:16", "Labels": {
            "com.docker.compose.project": "skybuild-pilot", "com.docker.compose.service": "db"},
            "Env": ["PGDATA=/var/lib/postgresql/data",
                    "POSTGRES_PASSWORD_FILE=/run/secrets/admin-password",
                    "UNRELATED_SECRET=never-print-this-password"]},
        "Mounts": [
            {"Destination": "/var/lib/postgresql/data", "Source": str(state / "pgdata"),
             "Type": "bind", "RW": True},
            {"Destination": "/run/secrets/admin-password", "Source": str(state / "secrets/admin-password"),
             "Type": "bind", "RW": False}],
    }
    fixture = tmp_path / "container.json"
    fixture.write_text(json.dumps(container))
    trace = tmp_path / "trace.jsonl"
    binary = tmp_path / "bin"
    binary.mkdir()
    fake = '''#!PYTHON
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["STARTUP_TRACE"], "a") as stream:
    stream.write(json.dumps([name, *args]) + "\\n")
if name == "docker":
    if args[0] == "inspect":
        if "--format" not in args:
            print("[" + pathlib.Path(os.environ["STARTUP_FIXTURE"]).read_text() + "]")
        elif "Running" in args[args.index("--format") + 1]:
            print("false" if os.environ.get("STARTUP_STOPPED") else "true")
        else:
            print("healthy")
    elif args[0] == "exec":
        print(os.environ.get("STARTUP_SYSTEM_ID", "7694565377149345831"))
elif name == "curl":
    port = "8000" if ":8000/" in args[-1] else "8443"
    if os.environ.get("STARTUP_FAIL_PORT") == port:
        sys.exit(22)
    print('{"status":"ready"}')
'''.replace("PYTHON", sys.executable)
    for name in ("docker", "curl"):
        target = binary / name
        target.write_text(fake)
        target.chmod(0o755)
    # Poll delays have no behavior to simulate. Avoid 30 Python interpreter
    # launches in readiness-failure tests under the bounded gate.
    sleep = binary / "sleep"
    sleep.write_text("#!/bin/sh\nexit 0\n")
    sleep.chmod(0o755)
    environment = dict(os.environ, PATH=str(binary) + os.pathsep + os.environ["PATH"],
                       SKYBUILD_PILOT_STATE=str(state), STARTUP_FIXTURE=str(fixture),
                       STARTUP_TRACE=str(trace))

    def run(*, change=None, extra=None):
        if change:
            change(container)
            fixture.write_text(json.dumps(container))
        result = subprocess.run(["sh", str(script)], capture_output=True, text=True,
                                env=environment | (extra or {}), timeout=15)
        events = [json.loads(row) for row in trace.read_text().splitlines()]
        assert "never-print-this-password" not in result.stdout + result.stderr
        return result, events
    return run


def test_qualified_database_precedes_api_and_verified_api_precedes_workbench(startup):
    result, events = startup(extra={"STARTUP_STOPPED": "1"})
    assert result.returncode == 0, result.stderr
    assert ["docker", "start", DATABASE_ID] in events
    starts = [(index, event[-1]) for index, event in enumerate(events)
              if event[:2] == ["docker", "compose"]]
    assert [name for _, name in starts] == ["api", "workbench"]
    health = [(index, event) for index, event in enumerate(events) if event[0] == "curl"]
    assert starts[0][0] < health[0][0] < starts[1][0] < health[1][0]
    for _, command in health:
        assert "--cacert" in command and "--resolve" in command
        assert "--noproxy" in command and "--max-time" in command
        assert "--insecure" not in command and "-k" not in command
    assert all("db" not in event for _, event in enumerate(events) if event[:2] == ["docker", "compose"])


@pytest.mark.parametrize("change", [
    lambda row: row.update(Id="unexpected-container"),
    lambda row: row.update(Image="sha256:unqualified"),
    lambda row: row["Mounts"][0].update(Source="/tmp"),
    lambda row: row["Mounts"][0].update(RW=False),
    lambda row: row["Mounts"][1].update(RW=True),
    lambda row: row["Config"]["Env"].append("PGDATA=/unexpected/data"),
])
def test_identity_or_mount_or_environment_mismatch_has_no_start_effect(startup, change):
    result, events = startup(change=change)
    assert result.returncode != 0
    assert "not the qualified SkyBuild pilot" in result.stderr
    assert all(event[:2] not in (["docker", "start"], ["docker", "compose"]) for event in events)


def test_wrong_cluster_identity_prevents_api_start(startup):
    result, events = startup(extra={"STARTUP_SYSTEM_ID": "other-cluster"})
    assert result.returncode != 0
    assert "cluster identity" in result.stderr
    assert not any(event[:2] == ["docker", "compose"] for event in events)


def test_api_readiness_failure_never_starts_gateway(startup):
    result, events = startup(extra={"STARTUP_FAIL_PORT": "8000"})
    assert result.returncode != 0
    assert "SkyBuild API did not become TLS-verified ready" in result.stderr
    starts = [event[-1] for event in events if event[:2] == ["docker", "compose"]]
    assert starts == ["api"]
    assert len([event for event in events if event[0] == "curl"]) == 30


def test_gateway_readiness_failure_is_not_reported_as_success(startup):
    result, events = startup(extra={"STARTUP_FAIL_PORT": "8443"})
    assert result.returncode != 0
    assert "SkyBuild Workbench did not become TLS-verified ready" in result.stderr
    assert "are ready" not in result.stdout
    assert len([event for event in events if event[0] == "curl" and ":8443/" in event[-1]]) == 30
