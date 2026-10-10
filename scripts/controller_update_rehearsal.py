#!/usr/bin/env python3
"""Prepare and, only after explicit GO, execute an isolated controller-update rehearsal.

Preparation verifies published source and migration compatibility without side
effects. The separate execution mode is isolated and plan-bound; it cannot
connect to or change the accepted pilot.
"""
from __future__ import annotations

import argparse
from contextlib import suppress
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from uuid import uuid4


ACCEPTED_SOURCE = "7da0ef6542f2a9aeae6feac529eca63b0b2d5bca"
ACCEPTED_IMAGE = "sha256:77bc01222c9c018d44968198e26d0be4618825b35ba07bb8c993002bce4f51c5"
CANDIDATE_SOURCE = "ec3fa9c12f560d205ddf46c08ccaef47012341c2"
CANDIDATE_TREE = "ec84d8ed278d7d8fbf8e8bb219cb64ddb48f934f"
CANDIDATE_REF = "refs/heads/dev-006"
_SHA40 = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
RESOURCE_LIMITS = {
    "postgres": ("--memory=2g", "--cpus=1", "--pids-limit=128"),
    "api": ("--memory=1536m", "--cpus=1", "--pids-limit=128"),
    "probe": ("--memory=1g", "--cpus=0.5", "--pids-limit=128"),
}
BUILD_TIMEOUT_SECONDS = 900
BUILD_MEMORY = "1g"
BUILD_CPU_QUOTA = "50000"
BUILD_CPU_PERIOD = "100000"
BUILD_PID_LIMIT = 128


class PreparationError(ValueError):
    """Pinned preparation evidence is incomplete or inconsistent."""


def _git(checkout: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(checkout), *args],
                            text=True, capture_output=True, check=False, timeout=15)
    if result.returncode:
        raise PreparationError("Git could not verify the requested source pin")
    return result.stdout.strip()


def plan_digest(plan: dict) -> str:
    payload = (json.dumps(plan, sort_keys=True, allow_nan=False, separators=(",", ":")) + "\n").encode()
    return hashlib.sha256(payload).hexdigest()


def _read_go_record(path: Path) -> dict:
    path = Path(path)
    info = path.lstat()
    if (not path.is_absolute() or not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o077 or info.st_size > 16_384):
        raise PreparationError("Reviewed GO record must be an owned private regular file under 16 KiB")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or opened.st_ino != info.st_ino or opened.st_dev != info.st_dev:
            raise PreparationError("Reviewed GO record changed while opening")
        return json.load(stream)


def _blob(checkout: Path, revision: str, path: str) -> bytes:
    result = subprocess.run(["git", "-C", str(checkout), "show", f"{revision}:{path}"],
                            capture_output=True, check=False, timeout=15)
    if result.returncode or len(result.stdout) > 1_048_576:
        raise PreparationError(f"Pinned source file unavailable or over limit: {path}")
    return result.stdout


def schema_manifest(checkout: Path, revision: str) -> list[dict[str, object]]:
    """Return ordered migration identities from a pinned Git tree."""
    names = _git(checkout, "ls-tree", "-r", "--name-only", revision,
                 "--", "src/skybuild/migrations").splitlines()
    migrations = []
    for name in names:
        match = re.fullmatch(r"src/skybuild/migrations/(\d{3})_[A-Za-z0-9_]+\.sql", name)
        if match:
            version = int(match[1])
            migrations.append({"version": version,
                               "sha256": hashlib.sha256(_blob(checkout, revision, name)).hexdigest(),
                               "path": name})
    migrations.sort(key=lambda row: int(row["version"]))
    if [row["version"] for row in migrations] != list(range(1, 14)):
        raise PreparationError("Pinned source must contain the complete ordered migration set 001–013")
    return migrations


def _evidence_schema(evidence: dict) -> list[dict[str, object]]:
    raw = evidence.get("schema")
    if not isinstance(raw, list):
        raise PreparationError("Accepted-runtime evidence has no schema digest list")
    parsed = []
    for entry in raw:
        if not isinstance(entry, str) or ":" not in entry:
            raise PreparationError("Accepted-runtime schema digest list is malformed")
        version_text, digest = entry.split(":", 1)
        if not version_text.isdecimal() or not _SHA256.fullmatch(digest):
            raise PreparationError("Accepted-runtime schema digest list is malformed")
        parsed.append({"version": int(version_text), "sha256": digest})
    if [row["version"] for row in parsed] != list(range(1, 14)):
        raise PreparationError("Accepted runtime must be recorded at schema 001–013")
    return parsed


def prepare(checkout: Path, evidence_path: Path, candidate_source: str = CANDIDATE_SOURCE,
            candidate_tree: str = CANDIDATE_TREE, candidate_ref: str = CANDIDATE_REF) -> dict:
    checkout = checkout.resolve()
    if not _SHA40.fullmatch(candidate_source) or not _SHA40.fullmatch(candidate_tree):
        raise PreparationError("Candidate commit and tree must be full Git object IDs")
    if not re.fullmatch(r"refs/heads/(?:dev-[0-9]{3}|main)", candidate_ref):
        raise PreparationError("Candidate must be published on an accepted dev-NNN or main ref")
    if _git(checkout, "rev-parse", "--show-toplevel") != str(checkout):
        raise PreparationError("Use the owned checkout root")
    fetch_url = _git(checkout, "remote", "get-url", "origin")
    push_url = _git(checkout, "remote", "get-url", "--push", "origin")
    if fetch_url != "https://github.com/stonesky-ai/skybuild.git" or push_url != fetch_url:
        raise PreparationError("Checkout origin fetch and push URLs must identify the SkyBuild repository")
    if _git(checkout, "status", "--porcelain", "--untracked-files=all"):
        raise PreparationError("Pinned candidate checkout must be clean")
    if _git(checkout, "rev-parse", "HEAD") != candidate_source:
        raise PreparationError("Checkout is not the exact root-supplied published candidate")
    if _git(checkout, "rev-parse", "HEAD^{tree}") != candidate_tree:
        raise PreparationError("Candidate tree does not match the accepted bundle tree")
    if _git(checkout, "ls-remote", "--exit-code", "origin", candidate_ref) != f"{candidate_source}\t{candidate_ref}":
        raise PreparationError("Candidate publication ref does not resolve to the pinned source")

    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    # The accepted API source/image binding is supplied by the reviewed task
    # provenance (7da0ef6 + immutable image ID). `source_head` in the runtime
    # audit is the mounted workbench source, not the API image source.
    # The candidate is separately proven published above. Runtime audit may
    # predate it, so its candidate field is not used as a source of authority.
    api = [row for row in evidence.get("containers", [])
           if isinstance(row, dict) and row.get("name") == "/skybuild-pilot-api"]
    if len(api) != 1 or api[0].get("image") != ACCEPTED_IMAGE or api[0].get("running") is not True:
        raise PreparationError("Evidence does not identify the running accepted controller image")
    if evidence.get("schema_unchanged") is not True or evidence.get("database_unchanged") is not True:
        raise PreparationError("Accepted-runtime evidence must confirm unchanged schema and database")

    accepted = schema_manifest(checkout, ACCEPTED_SOURCE)
    candidate = schema_manifest(checkout, candidate_source)
    recorded = _evidence_schema(evidence)
    accepted_digests = [{"version": row["version"], "sha256": row["sha256"]} for row in accepted]
    candidate_digests = [{"version": row["version"], "sha256": row["sha256"]} for row in candidate]
    if accepted_digests != recorded or candidate_digests != recorded:
        raise PreparationError("Accepted and candidate migration digests must exactly match runtime schema 001–013")
    version = _blob(checkout, candidate_source, "src/skybuild/__init__.py").decode("utf-8")
    if not re.search(r"(?m)^__version__\s*=\s*['\"]\d+\.\d+\.\d+['\"]", version):
        raise PreparationError("Candidate package version is not identifiable")

    return {
        "task_id": "SKYBUILD-MVP-ISOLATED-CONTROLLER-REHEARSAL",
        "mode": "preparation_only_no_runtime_changes",
        "accepted_controller": {"source": ACCEPTED_SOURCE, "image": ACCEPTED_IMAGE},
        "candidate": {"published_ref": candidate_ref, "source": candidate_source,
                      "tree": candidate_tree,
                      "package_version": re.search(r"(?m)^__version__\s*=\s*['\"]([^'\"]+)", version)[1]},
        "schema": {"version": 13, "migration_digests_match": True,
                   "migrations": candidate_digests},
        "resource_budget": {
            "minimum_host_available_gib": 8,
            "peak_concurrent_limited_memory_gib": 4.5,
            "additional_host_headroom_gib": 3.5,
            "postgres": {"memory": "2g", "cpus": 1, "pids": 128},
            "api_or_candidate": {"memory": "1536m", "cpus": 1, "pids": 128},
            "one_shot_probe": {"memory": "1g", "cpus": 0.5, "pids": 128},
            "candidate_build": {"memory": BUILD_MEMORY, "cpu_quota": BUILD_CPU_QUOTA,
                                 "cpu_period": BUILD_CPU_PERIOD, "pids": BUILD_PID_LIMIT,
                                 "timeout_seconds": BUILD_TIMEOUT_SECONDS},
        },
        "runtime_gate": [
            "Use a new isolated Docker network and disposable PostgreSQL data directory; no live mounts, credentials, or published ports.",
            "Load representative synthetic task and Cord history through the pinned accepted controller image.",
            "Build the exact candidate source into a separately identified immutable image while polling accepted-controller health throughout the build.",
            "Record full image/container IDs and journal every stop, start, health acknowledgment, and database identity.",
            "With the accepted controller stopped, exercise candidate startup failure; manually restore the accepted controller and verify history and readiness.",
            "Stop and acknowledge the accepted controller before starting the candidate normally; verify readiness, authenticated identity, task history, and Cord history.",
            "On uncertain stop/start/cleanup or a second writer, retain evidence and fail the rehearsal without claiming recovery.",
        ],
        "limitations": ["No runtime proof was performed by this preparation command.",
                        "No live deployment, schema change, credential operation, or authority switch is authorized."],
    }


def _docker(*args: str, input_text: str | None = None, timeout: int = 120,
            check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["docker", *args], input=input_text, text=True,
                            capture_output=True, check=False, timeout=timeout)
    if check and result.returncode:
        raise PreparationError("Isolated Docker rehearsal command failed: " + args[0])
    return result


def _docker_run_prefix(profile: str, *mode: str) -> list[str]:
    if profile not in RESOURCE_LIMITS:
        raise PreparationError("Unknown rehearsal container resource profile")
    return ["run", "--pull=never", *RESOURCE_LIMITS[profile], *mode]


def _candidate_build_command(image: str, dockerfile: Path, context: Path) -> list[str]:
    return ["docker", "build", "--pull=false", "--no-cache", "--network=none",
            "--memory=" + BUILD_MEMORY, "--memory-swap=" + BUILD_MEMORY,
            "--cpu-period=" + BUILD_CPU_PERIOD, "--cpu-quota=" + BUILD_CPU_QUOTA,
            "--ulimit", f"nproc={BUILD_PID_LIMIT}:{BUILD_PID_LIMIT}",
            "--tag", image, "--file", str(dockerfile), str(context)]


def _image_details(reference: str, *, check: bool = True) -> tuple[str, dict]:
    result = _docker("image", "inspect", "--format",
                     '{{.Id}} {{json .Config.Labels}}', reference, check=check, timeout=10)
    if result.returncode:
        return "", {}
    fields = result.stdout.strip().split(" ", 1)
    if len(fields) != 2 or not re.fullmatch(r"sha256:[0-9a-f]{64}", fields[0]):
        raise PreparationError("Image identity or labels are ambiguous")
    try:
        labels = json.loads(fields[1]) or {}
    except json.JSONDecodeError as error:
        raise PreparationError("Image labels could not be verified") from error
    if not isinstance(labels, dict):
        raise PreparationError("Image labels could not be verified")
    return fields[0], labels


def _require_image_absent(reference: str) -> None:
    result = _docker("image", "inspect", reference, check=False, timeout=10)
    if result.returncode == 0:
        raise PreparationError("Unique rehearsal image reference already exists")
    if result.stderr.strip() not in {f"Error: No such object: {reference}",
                                     f"Error: No such image: {reference}"}:
        raise PreparationError("Docker could not establish that the rehearsal image reference is unused")


def _cleanup_candidate_image(reference: str, run_id: str, source: str, journal: "Journal",
                             expected_image_id: str | None = None) -> None:
    result = _docker("image", "inspect", "--format", '{{.Id}} {{json .Config.Labels}}',
                     reference, check=False, timeout=10)
    if result.returncode:
        if result.stderr.strip() in {f"Error: No such object: {reference}",
                                     f"Error: No such image: {reference}"}:
            journal.event("candidate_image_cleanup_acknowledged", image_reference=reference,
                          image_absent=True)
            return
        raise PreparationError("Docker could not establish whether the rehearsal image remains")
    fields = result.stdout.strip().split(" ", 1)
    if len(fields) != 2 or not re.fullmatch(r"sha256:[0-9a-f]{64}", fields[0]):
        raise PreparationError("Candidate image cleanup identity is ambiguous")
    image_id = fields[0]
    try:
        labels = json.loads(fields[1]) or {}
    except json.JSONDecodeError as error:
        raise PreparationError("Candidate image cleanup labels are ambiguous") from error
    if (not isinstance(labels, dict) or labels.get("skybuild.rehearsal.run-id") != run_id
            or labels.get("skybuild.rehearsal.source") != source):
        raise PreparationError("Candidate image is not owned by this rehearsal; it was retained")
    if expected_image_id is not None and image_id != expected_image_id:
        raise PreparationError("Candidate image reference no longer resolves to the journaled immutable ID")
    _docker("image", "rm", image_id, timeout=15)
    verify = _docker("image", "inspect", reference, check=False, timeout=10)
    if verify.returncode == 0 or verify.stderr.strip() not in {
            f"Error: No such object: {reference}", f"Error: No such image: {reference}"}:
        raise PreparationError("Candidate image removal was not verifiably acknowledged")
    journal.event("candidate_image_cleanup_acknowledged", image_reference=reference,
                  image_id=image_id)


def _psql_input(container: str, database: str, sql: str, *, timeout: int = 15) -> None:
    """Send SQL on stdin to psql inside the disposable PostgreSQL container."""
    _docker("exec", "-i", container, "psql", "-U", "postgres", "-d", database,
            "-v", "ON_ERROR_STOP=1", input_text=sql, timeout=timeout)


def _stop_build_process(process: subprocess.Popen) -> None:
    """Stop the local Docker build client and wait for it before cleanup proceeds."""
    if process.poll() is not None:
        return
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


class Journal:
    """Append-only, fsynced evidence. Never records passwords or auth tokens."""
    def __init__(self, path: Path, identity: dict):
        self.path = path
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.exists() or self.path.is_symlink():
            raise PreparationError("Evidence path already exists; use a fresh run ID")
        if self.path.parent.stat().st_mode & 0o077:
            raise PreparationError("Evidence directory must be private (mode 0700)")
        self.fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        self.event("run_started", **identity)

    def event(self, name: str, **fields) -> None:
        record = {"event": name, "at_unix": time.time(), **fields}
        payload = (json.dumps(record, sort_keys=True, allow_nan=False) + "\n").encode()
        os.write(self.fd, payload)
        os.fsync(self.fd)

    def close(self) -> None:
        os.close(self.fd)


def _inspect_container(name: str) -> dict:
    result = _docker("inspect", "--type", "container", name)
    rows = json.loads(result.stdout)
    if len(rows) != 1:
        raise PreparationError("Container identity is ambiguous")
    row = rows[0]
    return {"id": row["Id"], "image": row["Image"],
            "running": row["State"]["Running"], "status": row["State"]["Status"],
            "exit_code": row["State"].get("ExitCode"),
            "rehearsal_run_id": (row.get("Config", {}).get("Labels") or {}).get("skybuild.rehearsal.run-id")}


def _live_identity(name: str) -> dict:
    """Read only the container ID, image ID and running flag."""
    result = _docker("inspect", "--type", "container", "--format",
                     "{{.Id}} {{.Image}} {{.State.Running}}", name)
    fields = result.stdout.strip().split()
    if len(fields) != 3 or not re.fullmatch(r"[0-9a-f]{64}", fields[0]) \
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", fields[1]) or fields[2] not in {"true", "false"}:
        raise PreparationError("Live accepted controller identity is ambiguous")
    return {"id": fields[0], "image": fields[1], "running": fields[2] == "true"}


def _available_gib() -> float:
    for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / (1024 * 1024)
    raise PreparationError("Available host memory could not be measured")


def _postgres_system_id(container: str) -> str:
    result = _docker("exec", container, "pg_controldata", "/var/lib/postgresql/data", timeout=10)
    match = re.search(r"(?m)^Database system identifier:\s+(\d+)\s*$", result.stdout)
    if not match:
        raise PreparationError("Disposable database system identifier could not be verified")
    return match[1]


def _history_fingerprint(database_container: str, task_id: str, message_id: str) -> dict:
    if not re.fullmatch(r"rehearsal-task-[0-9a-f]+", task_id) or not re.fullmatch(r"[0-9a-f-]{36}", message_id):
        raise PreparationError("Synthetic history identity is malformed")
    task_sql = ("SELECT COALESCE(json_agg(to_jsonb(j) ORDER BY event_id), '[]'::json)::text "
                "FROM skybuild.task_journal j WHERE task_id='" + task_id + "'")
    cord_sql = ("SELECT COALESCE(json_agg(to_jsonb(j) ORDER BY event_id), '[]'::json)::text "
                "FROM skybuild.cord_journal j WHERE message_id='" + message_id + "'")
    task_result = _docker("exec", database_container, "psql", "-U", "postgres", "-d",
                          "skybuild_rehearsal", "-A", "-t", "-c", task_sql, timeout=10)
    cord_result = _docker("exec", database_container, "psql", "-U", "postgres", "-d",
                          "skybuild_rehearsal", "-A", "-t", "-c", cord_sql, timeout=10)
    try:
        task_rows = json.loads(task_result.stdout)
        cord_rows = json.loads(cord_result.stdout)
    except json.JSONDecodeError as error:
        raise PreparationError("Synthetic journal rows could not be read from isolated PostgreSQL") from error
    if not task_rows or len(cord_rows) != 1:
        raise PreparationError("Synthetic task or Cord journal record is missing")
    return {"task_journal_rows": len(task_rows),
            "task_journal_sha256": hashlib.sha256(task_result.stdout.strip().encode()).hexdigest(),
            "cord_journal_rows": len(cord_rows),
            "cord_journal_sha256": hashlib.sha256(cord_result.stdout.strip().encode()).hexdigest()}


def _image_id(reference: str) -> str:
    result = _docker("image", "inspect", "--format", "{{.Id}}", reference)
    value = result.stdout.strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise PreparationError("Image identity is not immutable")
    return value


def _wait_api(name: str, journal: Journal, *, timeout: float = 45) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = _docker("exec", name, "python", "-c",
                         "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health/ready',timeout=2).read().decode())",
                         check=False, timeout=5)
        if result.returncode == 0 and result.stdout.strip() == '{"status":"ready"}':
            info = _inspect_container(name)
            journal.event("health_ready", container=name, container_id=info["id"], image_id=info["image"])
            return
        time.sleep(1)
    raise PreparationError(f"Controller readiness timed out: {name}")


def _api_json(container: str, method: str, path: str, token: str,
              body: dict | None = None) -> dict | list:
    program = (
        "import json,sys,urllib.request; "
        "method,path,token,raw=sys.argv[1:]; "
        "body=None if raw=='-' else raw.encode(); "
        "request=urllib.request.Request('http://127.0.0.1:8000'+path,data=body,method=method,"
        "headers={'Authorization':'Bearer '+token,'Idempotency-Key':'rehearsal-'+__import__('uuid').uuid4().hex,"
        "'Content-Type':'application/json'}); "
        "print(urllib.request.urlopen(request,timeout=5).read().decode())"
    )
    raw = "-" if body is None else json.dumps(body, separators=(",", ":"))
    result = _docker("exec", container, "python", "-c", program, method, path, token, raw,
                     timeout=10, check=False)
    if result.returncode:
        raise PreparationError("Synthetic authenticated API probe failed")
    return json.loads(result.stdout)


def _provision_principals(network: str, accepted_image: str, admin_dsn: str, runtime_role: str,
                          project: str, owner: str, owner_token: str,
                          worker: str, worker_token: str) -> None:
    program = (
        "from skybuild.store import Store; from skybuild.runtime_role import audit_runtime_role,provision_runtime_role; "
        "import os,psycopg; "
        "c=psycopg.connect(os.environ['SKYBUILD_ADMIN_DSN']); "
        "q=provision_runtime_role(c,os.environ['SKYBUILD_EXPECTED_DATABASE'],os.environ['RUNTIME_ROLE']); "
        "assert q['ok']; c.commit(); a=audit_runtime_role(c,os.environ['SKYBUILD_EXPECTED_DATABASE'],os.environ['RUNTIME_ROLE']); "
        "assert a['ok']; c.close(); "
        "s=Store(os.environ['SKYBUILD_ADMIN_DSN'],os.environ['SKYBUILD_EXPECTED_DATABASE']); "
        "s.provision_principal(os.environ['OWNER_ID'],os.environ['OWNER_TOKEN'],is_admin=True); "
        "s.provision_principal(os.environ['WORKER_ID'],os.environ['WORKER_TOKEN'],grants={os.environ['PROJECT']:["
        "'tasks:read','tasks:write','cord:read','cord:send','cord:handle']})"
    )
    _docker(*_docker_run_prefix("probe", "--rm"), "--network", network,
            "--env", "SKYBUILD_ADMIN_DSN=" + admin_dsn,
            "--env", "SKYBUILD_EXPECTED_DATABASE=skybuild_rehearsal",
            "--env", "RUNTIME_ROLE=" + runtime_role,
            "--env", "OWNER_ID=" + owner, "--env", "OWNER_TOKEN=" + owner_token,
            "--env", "WORKER_ID=" + worker, "--env", "WORKER_TOKEN=" + worker_token,
            "--env", "PROJECT=" + project, accepted_image, "python", "-c", program,
            timeout=30)


def _candidate_compatibility_probe(network: str, image_id: str, dsn: str) -> dict:
    """Load the candidate package and check its read-only schema/protocol contract."""
    program = (
        "import json,os; from skybuild.api import create_app; from skybuild.store import Store; "
        "s=Store(os.environ['SKYBUILD_DSN'],os.environ['SKYBUILD_EXPECTED_DATABASE']); "
        "ready=s.readiness(); app=create_app(s); "
        "route=next(r for r in app.routes if r.path=='/version' and 'GET' in r.methods); "
        "print(json.dumps({'ready':ready,'version':route.endpoint()},sort_keys=True))"
    )
    result = _docker(*_docker_run_prefix("probe", "--rm"), "--network", network,
                     "--env", "SKYBUILD_DSN=" + dsn,
                     "--env", "SKYBUILD_EXPECTED_DATABASE=skybuild_rehearsal",
                     image_id, "python", "-c", program, timeout=30)
    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise PreparationError("Candidate compatibility probe returned invalid output") from error
    if response.get("ready") != {"ready": True, "schema_version": 13}:
        raise PreparationError("Candidate does not accept the isolated schema-013 database")
    version = response.get("version")
    if not isinstance(version, dict) or version.get("service") != "skybuild" or version.get("protocol") != "v1":
        raise PreparationError("Candidate API protocol contract is incompatible")
    return response


def _check_writers(names: list[str], expected: str | None, journal: Journal,
                   expected_id: str | None = None) -> None:
    active = []
    for name in names:
        result = _docker("inspect", "--type", "container", name, check=False, timeout=10)
        if result.returncode == 0:
            rows = json.loads(result.stdout)
            if len(rows) != 1 or rows[0].get("Name", "").lstrip("/") != name:
                raise PreparationError("Controller writer identity is ambiguous")
            row = rows[0]
            if row["State"]["Running"]:
                active.append((name, row["Id"]))
        elif result.stderr.strip() not in {
                f"Error: No such object: {name}", f"Error: No such container: {name}"}:
            raise PreparationError("Docker could not establish whether a controller writer exists")
    journal.event("writer_count_checked", active=active, expected=expected or "none")
    if (expected is None and active) or (expected is not None
            and (len(active) != 1 or active[0] != (expected, expected_id))):
        raise PreparationError("One-writer invariant failed or is uncertain")


def execute(checkout: Path, evidence_path: Path, go_path: Path, evidence_output: Path,
            candidate_source: str = CANDIDATE_SOURCE, candidate_tree: str = CANDIDATE_TREE,
            candidate_ref: str = CANDIDATE_REF) -> dict:
    """Run the explicitly GO-authorized, isolated runtime exercise."""
    plan = prepare(checkout, evidence_path, candidate_source, candidate_tree, candidate_ref)
    expected_plan_sha256 = plan_digest(plan)
    go = _read_go_record(go_path)
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    pg_rows = [row for row in evidence["containers"] if row.get("name") == "/skybuild-pilot-pg"]
    api_rows = [row for row in evidence["containers"] if row.get("name") == "/skybuild-pilot-api"]
    if len(pg_rows) != 1 or len(api_rows) != 1:
        raise PreparationError("Accepted runtime evidence lacks exact API/PostgreSQL identities")
    pg_image = pg_rows[0]["image"]
    if (go.get("decision") != "GO" or go.get("task_id") != plan["task_id"]
            or go.get("plan_sha256") != expected_plan_sha256
            or go.get("accepted_image") != ACCEPTED_IMAGE
            or go.get("postgres_image") != pg_image
            or go.get("candidate_source") != candidate_source
            or go.get("candidate_tree") != candidate_tree
            or not isinstance(go.get("reviewer"), str) or not go["reviewer"].strip()
            or not isinstance(go.get("root_authorizer"), str) or not go["root_authorizer"].strip()):
        raise PreparationError("A matching independent-review and root GO record is required")

    accepted_image = ACCEPTED_IMAGE
    run_id = uuid4().hex[:12]
    live_api = _live_identity("skybuild-pilot-api")
    live_pg = _live_identity("skybuild-pilot-pg")
    if (not live_api["running"] or live_api["image"] != accepted_image
            or live_api["id"] != api_rows[0].get("id")
            or not live_pg["running"] or live_pg["image"] != pg_image
            or live_pg["id"] != pg_rows[0].get("id")
            or go.get("accepted_api_container_id") != live_api["id"]
            or go.get("accepted_pg_container_id") != live_pg["id"]):
        raise PreparationError("Current accepted API/PostgreSQL IDs differ from reviewed GO or runtime evidence")
    journal = Journal(evidence_output, {"task_id": plan["task_id"], "run_id": run_id,
                                        "accepted_source": ACCEPTED_SOURCE,
                                        "accepted_image": accepted_image, "candidate_source": candidate_source,
                                        "candidate_tree": candidate_tree, "postgres_image": pg_image,
                                        "accepted_api_container_id": live_api["id"],
                                        "accepted_pg_container_id": live_pg["id"],
                                        "reviewer": go["reviewer"], "root_authorizer": go["root_authorizer"]})
    network = "skybuild-mvp-rehearsal-net-" + run_id
    pg = "skybuild-mvp-rehearsal-pg-" + run_id
    old = "skybuild-mvp-rehearsal-accepted-" + run_id
    failed = "skybuild-mvp-rehearsal-failed-" + run_id
    candidate = "skybuild-mvp-rehearsal-candidate-" + run_id
    names = [old, failed, candidate]
    private_dir = Path(tempfile.mkdtemp(prefix="skybuild-mvp-rehearsal-"))
    private_dir.chmod(0o700)
    volume = "skybuild-mvp-rehearsal-data-" + run_id
    password_file = private_dir / "pg-password"
    password = secrets.token_urlsafe(32)
    password_file.write_text(password + "\n", encoding="ascii")
    password_file.chmod(0o600)
    admin_dsn = f"postgresql://postgres:{password}@db:5432/skybuild_rehearsal"
    runtime_role = "runtime_" + run_id
    runtime_password = secrets.token_urlsafe(32)
    runtime_dsn = f"postgresql://{runtime_role}:{runtime_password}@db:5432/skybuild_rehearsal"
    owner, owner_token = "rehearsal-owner-" + run_id, secrets.token_urlsafe(32)
    worker, worker_token = "rehearsal-worker-" + run_id, secrets.token_urlsafe(32)
    project, task = "rehearsal-project-" + run_id, "rehearsal-task-" + run_id
    candidate_image = "skybuild-mvp-rehearsal:" + run_id
    result = {"ok": False}
    cleanup_errors = []
    created_containers: dict[str, str] = {}
    network_id = None
    network_owned = False
    volume_owned = False
    candidate_image_id = None
    build_process = None

    def create_container(name: str, *args: str, profile: str = "api", timeout: int = 30) -> dict:
        launched = _docker(*_docker_run_prefix(profile, "-d"), "--name", name, "--label",
                           "skybuild.rehearsal.run-id=" + run_id, *args, timeout=timeout)
        container_id = launched.stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{64}", container_id):
            raise PreparationError("Docker did not return a full container ID")
        created_containers[name] = container_id
        info = _inspect_container(name)
        if info["id"] != container_id or info["rehearsal_run_id"] != run_id:
            raise PreparationError("New container identity did not match this rehearsal")
        return info

    try:
        available = _available_gib()
        journal.event("host_memory_preflight", available_gib=round(available, 2), reserve_gib=8)
        if available < 8:
            raise PreparationError("Isolated rehearsal requires at least 8 GiB available RAM")
        if _image_id(accepted_image) != accepted_image or _image_id(pg_image) != pg_image:
            raise PreparationError("Pinned accepted API or PostgreSQL image is not present locally by exact ID")
        journal.event("images_pinned", accepted_image=accepted_image, postgres_image=pg_image)
        network_result = _docker("network", "create", "--internal", "--label",
                                 "skybuild.rehearsal.run-id=" + run_id, network)
        network_id = network_result.stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{64}", network_id):
            raise PreparationError("Docker did not return a full isolated network ID")
        network_identity = _docker("network", "inspect", "--format",
                                   '{{.Id}} {{index .Labels "skybuild.rehearsal.run-id"}}', network).stdout.strip().split()
        if network_identity != [network_id, run_id]:
            raise PreparationError("Isolated network identity is not owned by this rehearsal")
        network_owned = True
        journal.event("network_created", network=network, internal=True)
        volume_result = _docker("volume", "create", "--label",
                                "skybuild.rehearsal.run-id=" + run_id, volume)
        if volume_result.stdout.strip() != volume:
            raise PreparationError("Docker did not confirm the rehearsal database volume name")
        volume_labels = _docker("volume", "inspect", "--format",
                                '{{index .Labels "skybuild.rehearsal.run-id"}}', volume).stdout.strip()
        if volume_labels != run_id:
            raise PreparationError("Database volume is not owned by this rehearsal")
        volume_owned = True
        journal.event("database_volume_created", volume=volume)
        _require_image_absent(candidate_image)
        pg_info = create_container(pg, "--network", network, "--network-alias", "db",
                "--mount", f"type=volume,src={volume},dst=/var/lib/postgresql/data",
                "--mount", f"type=bind,src={password_file},dst=/run/secrets/admin-password,readonly",
                "--env", "POSTGRES_PASSWORD_FILE=/run/secrets/admin-password", pg_image,
                profile="postgres", timeout=30)
        pg_id = pg_info["id"]
        journal.event("postgres_started", container=pg, container_id=pg_id, image_id=pg_image)
        ready_deadline = time.monotonic() + 45
        while time.monotonic() < ready_deadline:
            probe = _docker("exec", pg, "pg_isready", "-U", "postgres", "-d", "postgres",
                            timeout=5, check=False)
            if probe.returncode == 0:
                break
            time.sleep(1)
        else:
            raise PreparationError("Disposable PostgreSQL did not become ready")
        database_system_id = _postgres_system_id(pg)
        journal.event("database_identity_verified", container_id=pg_id,
                      image_id=pg_image, system_identifier=database_system_id)
        _docker("exec", pg, "psql", "-U", "postgres", "-d", "postgres", "-v", "ON_ERROR_STOP=1",
                "-c", "CREATE DATABASE skybuild_rehearsal", timeout=10)
        _psql_input(pg, "skybuild_rehearsal",
                    "CREATE SCHEMA skybuild; SET search_path TO skybuild, pg_catalog; "
                    "CREATE TABLE schema_migrations (version integer PRIMARY KEY, digest text NOT NULL);\n",
                    timeout=10)
        _docker("exec", pg, "psql", "-U", "postgres", "-d", "postgres", "-v", "ON_ERROR_STOP=1",
                "-c", f'CREATE ROLE "{runtime_role}" LOGIN PASSWORD \'{runtime_password}\'', timeout=10)
        for migration in schema_manifest(checkout, candidate_source):
            sql = _blob(checkout, candidate_source, migration["path"]).decode("utf-8")
            _psql_input(pg, "skybuild_rehearsal", "SET search_path TO skybuild, pg_catalog;\n" + sql,
                        timeout=15)
            insert = ("SET search_path TO skybuild, pg_catalog; INSERT INTO schema_migrations "
                      f"VALUES ({migration['version']}, '{migration['sha256']}');\n")
            _psql_input(pg, "skybuild_rehearsal", insert, timeout=10)
        journal.event("schema_initialized", schema_version=13,
                      migration_digests=[row["sha256"] for row in plan["schema"]["migrations"]],
                      runtime_role=runtime_role)
        _provision_principals(network, accepted_image, admin_dsn, runtime_role,
                              project, owner, owner_token,
                              worker, worker_token)
        old_info = create_container(old, "--network", network, "--env", "SKYBUILD_DSN=" + runtime_dsn,
                                    "--env", "SKYBUILD_EXPECTED_DATABASE=skybuild_rehearsal",
                                    accepted_image, timeout=30)
        old_id = old_info["id"]
        journal.event("accepted_controller_started", container=old, container_id=old_id,
                      image_id=accepted_image)
        _wait_api(old, journal)
        project_path = "/api/v1/projects/" + project
        task_result = _api_json(old, "POST", project_path + "/tasks", owner_token,
                                {"task_id": task, "title": "Synthetic update history",
                                 "description": "Isolated controller rehearsal; no production work."})
        cord_result = _api_json(old, "POST", project_path + "/cord/messages", owner_token,
                                {"recipient": worker, "subject": "Synthetic retained message",
                                 "body": "Isolated controller rehearsal."})
        inbox_before = _api_json(old, "GET", project_path + "/cord/inbox", worker_token)
        message_id = cord_result.get("message_id") if isinstance(cord_result, dict) else None
        if (not isinstance(task_result, dict) or task_result.get("task_id") != task
                or not isinstance(inbox_before, list) or not any(row.get("message_id") == message_id for row in inbox_before)):
            raise PreparationError("Synthetic task/Cord history was not visible through the accepted API")
        journal.event("synthetic_history_seeded", project_id=project, task_id=task,
                      cord_message_id=message_id)
        history_before = _history_fingerprint(pg, task, message_id)
        journal.event("synthetic_journals_fingerprinted", phase="accepted", **history_before)

        with tempfile.TemporaryDirectory(prefix="skybuild-candidate-context-") as context_name:
            context = Path(context_name)
            archive = subprocess.run(["git", "-C", str(checkout), "archive", candidate_source,
                                      "src/skybuild"], capture_output=True, check=False, timeout=20)
            if archive.returncode:
                raise PreparationError("Pinned candidate source archive could not be read")
            import tarfile
            import io
            with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as tar:
                for member in tar.getmembers():
                    if member.name.startswith("/") or ".." in Path(member.name).parts:
                        raise PreparationError("Candidate archive path escaped build context")
                tar.extractall(context, filter="data")
            (context / "Dockerfile").write_text(
                f"FROM {accepted_image}\nCOPY src/skybuild /candidate/skybuild\n"
                f"LABEL skybuild.rehearsal.run-id={run_id} skybuild.rehearsal.source={candidate_source}\n"
                "ENV PYTHONPATH=/candidate\n"
                "RUN python -c 'import hashlib; value=b\"skybuild-controller-update\"; "
                "assert sum(hashlib.sha256(value).digest()[0] for _ in range(5000000)) >= 0'\n",
                encoding="ascii")
            build_process = subprocess.Popen(
                _candidate_build_command(candidate_image, context / "Dockerfile", context),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            probes = 0
            build_returncode = None
            build_deadline = time.monotonic() + BUILD_TIMEOUT_SECONDS
            try:
                while build_process.poll() is None:
                    if time.monotonic() >= build_deadline:
                        raise PreparationError("Candidate build exceeded its reviewed time limit")
                    probe = _docker("exec", old, "python", "-c",
                                    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready',timeout=2).read()",
                                    check=False, timeout=5)
                    if probe.returncode:
                        raise PreparationError("Accepted controller stopped responding during candidate build")
                    probes += 1
                    journal.event("accepted_responsive_during_build", container_id=old_id, probe=probes)
                    time.sleep(1)
            finally:
                _stop_build_process(build_process)
                build_returncode = build_process.returncode
                journal.event("candidate_build_process_reaped", returncode=build_returncode,
                              completed=build_returncode == 0)
                build_process = None
            if build_returncode != 0 or probes < 1:
                raise PreparationError("Pinned isolated candidate image build failed or was not observed")
        candidate_image_id, candidate_labels = _image_details(candidate_image)
        if (candidate_labels.get("skybuild.rehearsal.run-id") != run_id
                or candidate_labels.get("skybuild.rehearsal.source") != candidate_source):
            raise PreparationError("Candidate image labels do not match this rehearsal and source pin")
        if candidate_image_id != _image_id(candidate_image):
            raise PreparationError("Candidate image identity changed after build")
        candidate_id = candidate_image_id
        journal.event("candidate_built", image_reference=candidate_image, candidate_image_id=candidate_id,
                      base_image_id=accepted_image, source=candidate_source, tree=candidate_tree)
        _check_writers(names, expected=old, journal=journal, expected_id=old_id)
        accepted_version = _api_json(old, "GET", "/version", owner_token)
        candidate_contract = _candidate_compatibility_probe(network, candidate_id, runtime_dsn)
        if (accepted_version.get("service") != candidate_contract["version"].get("service")
                or accepted_version.get("protocol") != candidate_contract["version"].get("protocol")):
            raise PreparationError("Candidate protocol differs from the accepted controller")
        journal.event("candidate_compatibility_verified_before_stop",
                      accepted_protocol=accepted_version["protocol"],
                      candidate_protocol=candidate_contract["version"]["protocol"],
                      candidate_package_version=candidate_contract["version"]["version"],
                      schema_version=candidate_contract["ready"]["schema_version"],
                      accepted_container_id=old_id, candidate_image_id=candidate_id)

        _docker("stop", "--time", "10", old, timeout=20)
        if _inspect_container(old)["running"]:
            raise PreparationError("Accepted controller stop was not acknowledged")
        journal.event("accepted_controller_stopped", container=old, container_id=old_id)
        _check_writers(names, expected=None, journal=journal)

        failed_started = create_container(failed, "--network", network,
                "--env", "SKYBUILD_DSN=" + runtime_dsn,
                "--env", "SKYBUILD_EXPECTED_DATABASE=skybuild_rehearsal",
                "--entrypoint", "/bin/false", candidate_id, timeout=20)
        failed_info = failed_started
        failed_deadline = time.monotonic() + 10
        while failed_info["running"] and time.monotonic() < failed_deadline:
            time.sleep(0.1)
            failed_info = _inspect_container(failed)
        journal.event("candidate_failure_injected", container=failed, container_id=failed_info["id"],
                      image_id=candidate_id, exit_code=failed_info["exit_code"])
        if failed_info["running"] or failed_info["status"] != "exited" or failed_info["exit_code"] != 1:
            raise PreparationError("Candidate failure injection did not fail closed")
        _check_writers(names, expected=None, journal=journal)
        recovery_command = f'docker start "{old_id}"'
        journal.event("manual_recovery_requested", container=old, container_id=old_id,
                      exact_operator_command=recovery_command)
        print("Candidate failed in the isolated rehearsal. Manually recover the accepted controller with:",
              recovery_command, file=sys.stderr, flush=True)
        acknowledgment = input(f"After the command succeeds, type RECOVERED {old_id}: ").strip()
        if acknowledgment != f"RECOVERED {old_id}":
            raise PreparationError("Manual recovery was not explicitly acknowledged")
        if not _inspect_container(old)["running"]:
            raise PreparationError("Manual recovery acknowledgment did not match a running accepted controller")
        journal.event("manual_recovery_start_acknowledged", container=old, container_id=old_id)
        _wait_api(old, journal)
        _check_writers(names, expected=old, journal=journal, expected_id=old_id)
        recovered_task = _api_json(old, "GET", project_path + "/tasks/" + task, owner_token)
        recovered_inbox = _api_json(old, "GET", project_path + "/cord/inbox", worker_token)
        if recovered_task.get("task_id") != task or not any(row.get("message_id") == message_id for row in recovered_inbox):
            raise PreparationError("Manual recovery lost synthetic task or Cord history")
        history_recovered = _history_fingerprint(pg, task, message_id)
        if history_recovered != history_before:
            raise PreparationError("Manual recovery changed synthetic task/Cord journal history")
        journal.event("manual_recovery_verified", container=old, container_id=old_id,
                      task_id=task, cord_message_id=message_id, history=history_recovered,
                      history_preserved=True)

        _docker("stop", "--time", "10", old, timeout=20)
        if _inspect_container(old)["running"]:
            raise PreparationError("Accepted controller stop before candidate promotion was not acknowledged")
        journal.event("accepted_controller_stopped_for_candidate", container=old, container_id=old_id)
        _check_writers(names, expected=None, journal=journal)
        candidate_container = create_container(candidate, "--network", network,
                "--env", "SKYBUILD_DSN=" + runtime_dsn,
                "--env", "SKYBUILD_EXPECTED_DATABASE=skybuild_rehearsal", candidate_id, timeout=20)
        journal.event("candidate_started", container=candidate, container_id=candidate_container["id"],
                      image_id=candidate_id)
        _wait_api(candidate, journal)
        _check_writers(names, expected=candidate, journal=journal, expected_id=candidate_container["id"])
        identity = _api_json(candidate, "GET", "/api/v1/me", owner_token)
        task_after = _api_json(candidate, "GET", project_path + "/tasks/" + task, owner_token)
        inbox_after = _api_json(candidate, "GET", project_path + "/cord/inbox", worker_token)
        if (identity.get("principal_id") != owner or task_after.get("task_id") != task
                or not any(row.get("message_id") == message_id for row in inbox_after)):
            raise PreparationError("Candidate did not preserve authenticated task/Cord authority")
        history_after = _history_fingerprint(pg, task, message_id)
        if history_after != history_before:
            raise PreparationError("Candidate changed synthetic task/Cord journal history")
        journal.event("candidate_verified", container=candidate, container_id=candidate_container["id"],
                      image_id=candidate_id, task_id=task, cord_message_id=message_id,
                      history=history_after,
                      task_history_preserved=True, cord_history_preserved=True,
                      candidate_ref=candidate_ref)
        result = {"ok": True, "candidate_image_id": candidate_id,
                  "accepted_container_id": old_id, "candidate_container_id": candidate_container["id"],
                  "postgres_container_id": pg_id, "database": "skybuild_rehearsal",
                  "database_system_identifier": database_system_id,
                  "schema_version": 13, "task_id": task, "cord_message_id": message_id,
                  "limitations": ["Synthetic disposable runtime only; no production restore or deployment proof."]}
        journal.event("run_passed", **result)
    except Exception as error:
        result = {"ok": False, "error": str(error)}
        with suppress(Exception):
            journal.event("run_failed", error=type(error).__name__, detail=str(error))
    finally:
        # Cleanup is fail-closed: every removal is individually journaled; any
        # uncertain Docker acknowledgment makes the result unsuccessful.
        if build_process is not None:
            try:
                _stop_build_process(build_process)
                journal.event("candidate_build_process_stopped", returncode=build_process.returncode)
            except Exception as error:
                cleanup_errors.append("candidate-build-process")
                with suppress(Exception):
                    journal.event("candidate_build_process_cleanup_unconfirmed", error=type(error).__name__)
        for name in [candidate, failed, old, pg]:
            container_id = created_containers.get(name)
            if container_id is None:
                continue
            try:
                inspected = _docker("inspect", "--type", "container", container_id, check=False, timeout=10)
                if inspected.returncode:
                    cleanup_errors.append(name)
                    journal.event("container_cleanup_unconfirmed", container=name, container_id=container_id,
                                  error="created container ID unavailable")
                    continue
                row = json.loads(inspected.stdout)[0]
                if (row["Id"] != container_id
                        or (row.get("Config", {}).get("Labels") or {}).get("skybuild.rehearsal.run-id") != run_id):
                    cleanup_errors.append(name)
                    journal.event("container_cleanup_unconfirmed", container=name, container_id=container_id,
                                  error="container ownership identity changed")
                    continue
                if row["State"]["Running"]:
                    _docker("stop", "--time", "10", container_id, timeout=20)
                _docker("rm", container_id, timeout=15)
                journal.event("container_cleanup_acknowledged", container=name, container_id=row["Id"])
            except Exception as error:
                cleanup_errors.append(name)
                with suppress(Exception):
                    journal.event("container_cleanup_unconfirmed", container=name, error=type(error).__name__)
        try:
            _cleanup_candidate_image(candidate_image, run_id, candidate_source, journal,
                                     candidate_image_id)
        except Exception as error:
            cleanup_errors.append(candidate_image)
            with suppress(Exception):
                journal.event("candidate_image_cleanup_unconfirmed", image_reference=candidate_image,
                              image_id=candidate_image_id, error=type(error).__name__)
        if network_owned:
            network_identity = _docker("network", "inspect", "--format",
                                       '{{.Id}} {{index .Labels "skybuild.rehearsal.run-id"}}',
                                       network, check=False, timeout=10)
            if network_identity.returncode == 0 and network_identity.stdout.strip().split() == [network_id, run_id]:
                removed = _docker("network", "rm", network_id, check=False, timeout=10)
                if removed.returncode == 0:
                    journal.event("network_cleanup_acknowledged", network_id=network_id)
                else:
                    cleanup_errors.append(network)
                    with suppress(Exception):
                        journal.event("network_cleanup_unconfirmed", network_id=network_id)
            else:
                cleanup_errors.append(network)
                with suppress(Exception):
                    journal.event("network_cleanup_unconfirmed", network_id=network_id,
                                  error="network ownership identity changed")
        if volume_owned:
            volume_identity = _docker("volume", "inspect", "--format",
                                      '{{.Name}} {{index .Labels "skybuild.rehearsal.run-id"}}',
                                      volume, check=False, timeout=10)
            if volume_identity.returncode == 0 and volume_identity.stdout.strip().split() == [volume, run_id]:
                volume_removed = _docker("volume", "rm", volume, check=False, timeout=10)
                if volume_removed.returncode == 0:
                    journal.event("database_volume_cleanup_acknowledged", volume=volume)
                else:
                    cleanup_errors.append(volume)
                    with suppress(Exception):
                        journal.event("database_volume_cleanup_unconfirmed", volume=volume)
            else:
                cleanup_errors.append(volume)
                with suppress(Exception):
                    journal.event("database_volume_cleanup_unconfirmed", volume=volume,
                                  error="volume ownership identity changed")
        try:
            shutil.rmtree(private_dir)
            if private_dir.exists():
                raise OSError("private rehearsal directory still exists")
            journal.event("synthetic_secret_cleanup_acknowledged")
        except OSError as error:
            cleanup_errors.append(str(private_dir))
            with suppress(Exception):
                journal.event("synthetic_secret_cleanup_unconfirmed", error=type(error).__name__)
        if cleanup_errors:
            result["ok"] = False
            result["cleanup_unconfirmed"] = cleanup_errors
        with suppress(Exception):
            journal.event("run_finished", ok=result["ok"], cleanup_unconfirmed=cleanup_errors)
        journal.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--accepted-runtime-evidence", type=Path, required=True)
    parser.add_argument("--candidate-source", default=CANDIDATE_SOURCE)
    parser.add_argument("--candidate-tree", default=CANDIDATE_TREE)
    parser.add_argument("--candidate-ref", default=CANDIDATE_REF)
    parser.add_argument("--execute", action="store_true",
                        help="Run isolated rehearsal only with a matching independent-review/root GO record")
    parser.add_argument("--reviewed-go-record", type=Path)
    parser.add_argument("--evidence-output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.execute:
            if not args.reviewed_go_record or not args.evidence_output:
                raise PreparationError("Execution requires reviewed GO record and fresh evidence-output path")
            result = execute(args.checkout, args.accepted_runtime_evidence,
                             args.reviewed_go_record, args.evidence_output,
                             args.candidate_source, args.candidate_tree, args.candidate_ref)
        else:
            result = prepare(args.checkout, args.accepted_runtime_evidence,
                             args.candidate_source, args.candidate_tree, args.candidate_ref)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(json.dumps({"prepared": False, "error": str(error)}, sort_keys=True), file=sys.stderr)
        return 2
    rendered = {"executed": True, **result} if args.execute else {"prepared": True, **result}
    if not args.execute:
        rendered["plan_sha256"] = plan_digest(result)
    print(json.dumps(rendered, sort_keys=True, indent=2))
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
