"""Explicit, one-time provisioning for the dedicated manual pilot database.

Run only after the reviewed operator preflight and Docker Compose database start.
This script refuses an existing application database or runtime role. It never
connects to, inspects, or modifies a SkyKeep database.
"""

import argparse
import json
import os
import secrets
import stat
import subprocess
from pathlib import Path
from urllib.parse import quote

import psycopg
from psycopg import sql

from skybuild.runtime_role import audit_runtime_role, provision_runtime_role
from skybuild.store import Store


DATABASE = "skybuild_pilot"
ROLE = "skybuild_pilot_runtime"
PROJECT = "skybuild"
WORKERS = ("wonko", "wowbagger")
CONTAINER = "skybuild-pilot-pg"


def _state_dir(path: Path, *, create: bool) -> Path:
    if not path.is_absolute() or path.is_symlink():
        raise ValueError("State directory must be an absolute non-symlink path")
    if create:
        path.mkdir(mode=0o700)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError("State directory must be owned by this user and mode 0700")
    return path


def _write_new(path: Path, content: str, mode: int) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, mode)
    try:
        os.fchmod(descriptor, mode)
        os.write(descriptor, content.encode("ascii"))
    finally:
        os.close(descriptor)


def _read_secret(path: Path, *, mode: int) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o777 != mode:
            raise ValueError("Secret file ownership or mode is invalid")
        data = os.read(descriptor, 256).decode("ascii").strip()
    finally:
        os.close(descriptor)
    if len(data) < 32 or len(data) > 200 or not all(char.isalnum() or char in "-_" for char in data):
        raise ValueError("Secret file content is invalid")
    return data


def init_secrets(state: Path) -> dict:
    state = _state_dir(state, create=True)
    secret_dir = state / "secrets"
    secret_dir.mkdir(mode=0o700)
    if secret_dir.is_symlink() or secret_dir.stat().st_mode & 0o077:
        raise ValueError("Secret directory must be private")
    names = ("admin-password", "runtime-password", "pilot_owner-token", "pilot_dispatcher-token",
             *(f"{worker}-token" for worker in WORKERS))
    if any((secret_dir / name).exists() for name in names):
        raise ValueError("Existing pilot secrets require reconciliation; refusing partial overwrite")
    for name in names:
        # PostgreSQL's container user must read its mounted password file. The
        # containing host directory is 0700 and only the database mounts it.
        mode = 0o644 if name == "admin-password" else 0o600
        _write_new(secret_dir / name, secrets.token_urlsafe(48) + "\n", mode)
    (state / "pgdata").mkdir(mode=0o700, exist_ok=True)
    return {"initialized": True, "state_dir": str(state), "secret_names": list(names)}


def _dedicated_container(state: Path) -> str:
    result = subprocess.run(["docker", "container", "inspect", CONTAINER,
                             "--format", "{{json .}}"],
                            capture_output=True, text=True, check=False, timeout=10)
    if result.returncode != 0:
        raise ValueError("Pilot PostgreSQL container is unavailable")
    inspected = json.loads(result.stdout)
    labels = inspected["Config"]["Labels"]
    if labels.get("com.docker.compose.project") != "skybuild-pilot" or labels.get("com.docker.compose.service") != "db":
        raise ValueError("Container is not owned by the SkyBuild pilot Compose project")
    if not inspected["State"]["Running"] or inspected["State"].get("Health", {}).get("Status") != "healthy":
        raise ValueError("Pilot PostgreSQL container is not healthy")
    if inspected["Config"]["Image"] != "postgres:16":
        raise ValueError("Pilot PostgreSQL container uses an unexpected image")
    image = subprocess.run(["docker", "image", "inspect", "postgres:16", "--format", "{{.Id}}"],
                           capture_output=True, text=True, check=False, timeout=10)
    if image.returncode != 0 or inspected["Image"] != image.stdout.strip():
        raise ValueError("Pilot PostgreSQL image does not match the local reviewed image")
    expected_port = [{"HostIp": "127.0.0.1", "HostPort": "55432"}]
    if (inspected["HostConfig"]["PortBindings"] != {"5432/tcp": expected_port}
            or inspected["NetworkSettings"]["Ports"].get("5432/tcp") != expected_port):
        raise ValueError("Pilot PostgreSQL does not own the expected loopback port")
    data = state / "pgdata"
    password = state / "secrets" / "admin-password"
    if data.is_symlink() or password.is_symlink() or not data.is_dir():
        raise ValueError("Pilot state mounts are not ordinary owned paths")
    expected_mounts = {
        (str(data.resolve()), "/var/lib/postgresql/data", True),
        (str(password.resolve()), "/run/secrets/admin-password", False),
    }
    mounts = {(mount["Source"], mount["Destination"], mount["RW"])
              for mount in inspected["Mounts"] if mount["Type"] == "bind"}
    if mounts != expected_mounts or len(inspected["Mounts"]) != 2:
        raise ValueError("Pilot PostgreSQL mounts do not match the requested state directory")
    if (inspected["HostConfig"]["Memory"] <= 0 or inspected["HostConfig"]["Memory"] > 768 * 1024 ** 2
            or inspected["HostConfig"]["PidsLimit"] <= 0 or inspected["HostConfig"]["PidsLimit"] > 128):
        raise ValueError("Pilot PostgreSQL resource limits are missing")
    identifier = subprocess.run(["docker", "exec", "--user", "postgres", CONTAINER, "psql",
                                 "--no-psqlrc", "--tuples-only", "--no-align", "--dbname=postgres",
                                 "--command=SELECT system_identifier FROM pg_control_system()"],
                                capture_output=True, text=True, check=False, timeout=10)
    if identifier.returncode != 0 or not identifier.stdout.strip().isdigit():
        raise ValueError("Pilot PostgreSQL system identifier is unavailable")
    return identifier.stdout.strip()


def _require_same_cluster(connection, expected_identifier: str) -> None:
    observed = connection.execute("SELECT system_identifier FROM pg_control_system()").fetchone()[0]
    if str(observed) != expected_identifier:
        raise ValueError("Loopback PostgreSQL listener is not the inspected pilot container")


def _dsn(password: str, database: str, *, host: str, port: int) -> str:
    user = "postgres" if database == "postgres" else ROLE
    return f"postgresql://{user}:{quote(password, safe='')}@{host}:{port}/{database}"


def provision(state: Path) -> dict:
    state = _state_dir(state, create=False)
    expected_identifier = _dedicated_container(state)
    secret_dir = state / "secrets"
    admin_password = _read_secret(secret_dir / "admin-password", mode=0o644)
    runtime_password = _read_secret(secret_dir / "runtime-password", mode=0o600)
    admin_postgres = _dsn(admin_password, "postgres", host="127.0.0.1", port=55432)
    # The target starts absent. Any partial prior attempt requires human
    # reconciliation; rerunning must not rotate credentials or duplicate setup.
    with psycopg.connect(admin_postgres, connect_timeout=5, autocommit=True) as connection:
        _require_same_cluster(connection, expected_identifier)
        if connection.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DATABASE,)).fetchone():
            raise ValueError("Pilot database already exists; inspect before retry")
        if connection.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (ROLE,)).fetchone():
            raise ValueError("Pilot runtime role already exists; inspect before retry")
        connection.execute(sql.SQL("CREATE DATABASE {} OWNER postgres").format(sql.Identifier(DATABASE)))
        connection.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
            sql.Identifier(ROLE), sql.Literal(runtime_password)))
    admin_database = f"postgresql://postgres:{quote(admin_password, safe='')}@127.0.0.1:55432/{DATABASE}"
    store = Store(admin_database, DATABASE)
    store.migrate()
    with psycopg.connect(admin_database, connect_timeout=5) as connection:
        qualified = provision_runtime_role(connection, DATABASE, ROLE)
    if not qualified["ok"]:
        raise ValueError("Restricted runtime role provisioning failed")
    store.provision_principal("pilot_owner", _read_secret(secret_dir / "pilot_owner-token", mode=0o600), is_admin=True)
    store.provision_principal("pilot_dispatcher", _read_secret(secret_dir / "pilot_dispatcher-token", mode=0o600),
                              grants={PROJECT: {"tasks:read", "cord:read", "cord:send", "cord:handle"}})
    scopes = {PROJECT: {"tasks:read", "cord:read", "cord:send", "cord:handle"}}
    for worker in WORKERS:
        store.provision_principal(worker, _read_secret(secret_dir / f"{worker}-token", mode=0o600), grants=scopes)
    with psycopg.connect(admin_database, connect_timeout=5) as connection:
        audit = audit_runtime_role(connection, DATABASE, ROLE)
    if not audit["ok"]:
        raise ValueError("Restricted runtime role audit failed")
    container_dsn = _dsn(runtime_password, DATABASE, host="db", port=5432)
    runtime_file = state / "runtime.env"
    _write_new(runtime_file, f"SKYBUILD_DSN={container_dsn}\nSKYBUILD_EXPECTED_DATABASE={DATABASE}\n", 0o600)
    return {"provisioned": True, "database": DATABASE, "runtime_role_audit": "passed",
            "principals": ["pilot_owner", "pilot_dispatcher", *WORKERS], "runtime_env": str(runtime_file)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init-secrets", "provision"))
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = init_secrets(args.state_dir) if args.command == "init-secrets" else provision(args.state_dir)
    except (OSError, ValueError, KeyError, TypeError, psycopg.Error, subprocess.TimeoutExpired):
        print(json.dumps({"ok": False, "reason": "Pilot setup failed; inspect owned state before retry"}))
        return 2
    print(json.dumps({"ok": True, **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
