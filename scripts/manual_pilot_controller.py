"""Read-only controller preflight for the first manual REST/Cord worker pilot.

This deliberately does not provision a database, alter Tailscale Serve, or start
workers. The matching operator runbook contains the explicit deployment steps.
"""

import argparse
import hashlib
import os
import json
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path


DATABASE_PORT = 55432
API_PORT = 8000
DATABASE_CONTAINER = "skybuild-pilot-pg"
POSTGRES_IMAGE = "postgres:16"
MIN_AVAILABLE_GIB = 10
MIN_DISK_GIB = 4


def _command(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, check=False, timeout=10)


def _available_gib() -> float:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / (1024 * 1024)
    raise ValueError("MemAvailable unavailable")


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def preflight(checkout: Path, expected_sha: str, published_ref: str) -> dict:
    """Return actionable checks without mutating the host or printing secrets."""
    from manual_pilot_tls import valid_publication_ref

    checks: dict[str, dict] = {}

    def add(name: str, ok: bool, detail: str) -> None:
        checks[name] = {"ok": bool(ok), "detail": detail}

    checkout = checkout.resolve()
    git = _command("git", "-C", str(checkout), "rev-parse", "--show-toplevel")
    remote = _command("git", "-C", str(checkout), "remote", "get-url", "origin")
    push_remote = _command("git", "-C", str(checkout), "remote", "get-url", "--push", "origin")
    add("checkout", git.returncode == 0 and Path(git.stdout.strip()) == checkout
        and remote.stdout.strip() == "https://github.com/stonesky-ai/skybuild.git"
        and push_remote.stdout.strip() == remote.stdout.strip(),
        "Require exact SkyBuild checkout and matching origin fetch/push URLs")
    head = _command("git", "-C", str(checkout), "rev-parse", "HEAD")
    status = _command("git", "-C", str(checkout), "status", "--porcelain", "--untracked-files=all")
    published = _command("git", "-C", str(checkout), "ls-remote", "--exit-code", "origin", published_ref)
    pinned = bool(re.fullmatch(r"[0-9a-f]{40}", expected_sha)) and valid_publication_ref(published_ref)
    add("published_clean_head", pinned and head.returncode == 0 and head.stdout.strip() == expected_sha
        and status.returncode == 0 and not status.stdout
        and published.returncode == 0 and published.stdout.strip() == f"{expected_sha}\t{published_ref}",
        "Require clean checkout at the exact approved, published dev-NNN or main SHA")

    available = _available_gib()
    add("memory", available >= MIN_AVAILABLE_GIB,
        f"{available:.1f} GiB available; require {MIN_AVAILABLE_GIB} GiB before setup")
    disk = shutil.disk_usage(checkout).free / (1024 ** 3)
    add("disk", disk >= MIN_DISK_GIB,
        f"{disk:.1f} GiB free at checkout; require {MIN_DISK_GIB} GiB")
    add("database_port", _port_free(DATABASE_PORT), f"127.0.0.1:{DATABASE_PORT} must be free")
    add("api_port", _port_free(API_PORT), f"127.0.0.1:{API_PORT} must be free")

    docker = _command("docker", "info", "--format", "{{.ServerVersion}}")
    add("docker", docker.returncode == 0, "Docker daemon must be available")
    compose = _command("docker", "compose", "version", "--short")
    add("compose", compose.returncode == 0, "Docker Compose plugin must be available")
    if docker.returncode == 0:
        image = _command("docker", "image", "inspect", POSTGRES_IMAGE, "--format", "{{.Id}}")
        add("postgres_image", image.returncode == 0,
            "Use locally available postgres:16 image; do not pull during setup")
        container = _command("docker", "container", "inspect", DATABASE_CONTAINER,
                             "--format", "{{.Id}}")
        add("container_name", container.returncode != 0,
            "Dedicated pilot container name must be unused; inspect existing container before retry")

    tailscale = _command("tailscale", "status", "--json")
    try:
        tail_status = json.loads(tailscale.stdout)
    except ValueError:
        tail_status = {}
    add("tailscale", tailscale.returncode == 0 and tail_status.get("BackendState") == "Running",
        "Tailscale must be running on controller")
    serve = _command("tailscale", "serve", "status", "--json")
    try:
        serve_status = json.loads(serve.stdout)
    except ValueError:
        serve_status = None
    add("serve_empty", serve.returncode == 0 and serve_status == {},
        "Existing Serve configuration requires manual ownership review before adding pilot route")

    return {"ready_for_operator_setup": all(row["ok"] for row in checks.values()),
            "checks": checks, "no_changes_made": True}



def _source_manifest(checkout: Path, revision: str) -> dict[str, str]:
    from manual_pilot_tls import command
    paths = command("git", "-C", str(checkout), "ls-tree", "-r", "--name-only", revision,
                    "--", "src/skybuild").splitlines()
    # Every tracked package file is shipped evidence, including browser assets.
    if not paths or len(paths) > 200:
        raise ValueError("Source manifest exceeds bounded package scope")
    manifest = {}
    for path in paths:
        result = _command("git", "-C", str(checkout), "show", f"{revision}:{path}")
        data = result.stdout.encode()
        if result.returncode or len(data) > 1048576:
            raise ValueError("Pinned package file is unavailable or exceeds its bound")
        manifest[path.removeprefix("src/skybuild/")] = hashlib.sha256(data).hexdigest()
    return manifest


SCHEMA_TRANSITIONS: dict[str, tuple[int, int, tuple[str, ...]]] = {
    "011-to-012": (11, 12, ("migrations/012_api_task_authority.sql",)),
    "012-to-013": (12, 13, ("migrations/013_petri_workflow.sql",)),
    "013-to-016": (13, 16, (
        "migrations/014_real_cpu_dispatch.sql",
        "migrations/015_trusted_integration.sql",
        "migrations/016_task_usage_history.sql",
    )),
}


def _verify_schema_transition(current: dict[str, str], candidate: dict[str, str],
                              transition: str) -> tuple[int, int]:
    if transition not in SCHEMA_TRANSITIONS:
        raise ValueError("Require an explicit reviewed schema transition")
    current_version, candidate_version, migrations = SCHEMA_TRANSITIONS[transition]
    old = {name: digest for name, digest in current.items() if name.startswith("migrations/")}
    new = {name: digest for name, digest in candidate.items() if name.startswith("migrations/")}
    versions = sorted(int(Path(name).name.split("_", 1)[0]) for name in old)
    registered_versions = [int(Path(name).name.split("_", 1)[0]) for name in migrations]
    expected_versions = list(range(current_version + 1, candidate_version + 1))
    if (versions != list(range(1, current_version + 1))
            or any(new.get(name) != digest for name, digest in old.items())
            or registered_versions != expected_versions
            or set(new) - set(old) != set(migrations)):
        raise ValueError(
            f"Require unchanged schema 001-{current_version:03} and only the exact reviewed "
            f"migration set through {candidate_version:03}"
        )
    return current_version, candidate_version


def _verify_schema_011_to_012(current: dict[str, str], candidate: dict[str, str]) -> None:
    """Preserve the historical operator contract."""
    _verify_schema_transition(current, candidate, "011-to-012")



def promotion_preflight(checkout: Path, expected_sha: str, published_ref: str, *,
                        current_sha: str, state_dir: Path, hostname: str, tailnet_ip: str,
                        api_container: str, db_container: str, api_image: str, system_id: str,
                        ca_pem_sha256: str, gateway_container: str | None = None,
                        gateway_image: str | None = None, compose_owner_path: Path | None = None,
                        gateway_compose_owner_path: Path | None = None,
                        schema_transition: str = "011-to-012") -> dict:
    """Read-only, exact-identity preflight for an explicitly reviewed promotion.

    Expected identities come from retained deployment evidence, not from blindly
    accepting whatever happens to own a container name or a loopback listener.
    No migration, credential change, image build or service action occurs here.
    """
    import psycopg
    from psycopg.conninfo import make_conninfo
    import manual_pilot_provision as provisioner
    import manual_pilot_tls as tls
    from skybuild.runtime_role import audit_runtime_role

    gateway_values = (gateway_container, gateway_image, compose_owner_path, gateway_compose_owner_path)
    gateway_mode = any(value is not None for value in gateway_values)
    if (gateway_mode and not all(value is not None for value in gateway_values)
            or not re.fullmatch(r"[0-9a-f]{40}", current_sha)
            or not all(re.fullmatch(r"[0-9a-f]{64}", value) for value in (api_container, db_container))
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", api_image)
            or (gateway_mode and (not re.fullmatch(r"[0-9a-f]{64}", gateway_container)
                                  or not re.fullmatch(r"sha256:[0-9a-f]{64}", gateway_image)))
            or not system_id.isdigit() or not re.fullmatch(r"[0-9a-f]{64}", ca_pem_sha256)):
        raise ValueError("Require retained exact source/container/image/cluster identities")
    if not tls.valid_publication_ref(published_ref):
        raise ValueError("Require the approved publication ref")
    checkout = checkout.absolute()
    if gateway_mode:
        if not compose_owner_path.is_absolute():
            raise ValueError("Require an absolute retained Compose ownership path")
        supplied_owner_path = compose_owner_path
        compose_owner_path = compose_owner_path.resolve(strict=True)
        if (supplied_owner_path != compose_owner_path
                or compose_owner_path.name != "manual-pilot"
                or compose_owner_path.parent.name != "ops"):
            raise ValueError("Require the explicitly retained Compose ownership path")
        if not gateway_compose_owner_path.is_absolute():
            raise ValueError("Require an absolute Workbench Compose ownership path")
        supplied_gateway_owner_path = gateway_compose_owner_path
        gateway_compose_owner_path = gateway_compose_owner_path.resolve(strict=True)
        if supplied_gateway_owner_path != gateway_compose_owner_path:
            raise ValueError("Require the exact Workbench Compose ownership path")
    tls.controller(checkout, expected_sha, state_dir, hostname, tailnet_ip,
                   expected_api_image=api_image, published_ref=published_ref,
                   compose_owner_path=compose_owner_path)
    if tls.command("git", "-C", str(checkout), "ls-remote", "--exit-code", "origin", published_ref) != \
            f"{expected_sha}\t{published_ref}":
        raise ValueError("Candidate differs from the exact approved published ref")
    tls.command("git", "-C", str(checkout), "merge-base", "--is-ancestor", current_sha, expected_sha)
    if _available_gib() < MIN_AVAILABLE_GIB or shutil.disk_usage(checkout).free < MIN_DISK_GIB * 1024**3:
        raise ValueError("Promotion preparation requires memory/disk headroom")
    if os.getpriority(os.PRIO_PROCESS, 0) < 10:
        raise ValueError("Run promotion preparation with nice -n 10")
    tls_report = tls.check(state_dir / "tls", hostname)
    if hashlib.sha256((state_dir / 'tls/ca.crt').read_bytes()).hexdigest() != ca_pem_sha256:
        raise ValueError("Pinned runtime CA differs from retained deployment evidence")
    if json.loads(tls.command("tailscale", "serve", "status", "--json")) != {}:
        raise ValueError("Unexpected Serve/Funnel configuration needs separate reconciliation")
    current = _source_manifest(checkout, current_sha)
    candidate = _source_manifest(checkout, expected_sha)
    current_version, candidate_version = _verify_schema_transition(current, candidate, schema_transition)
    old_migrations = {name: digest for name, digest in current.items() if name.startswith("migrations/")}
    old_names = sorted(old_migrations)

    inspected = {}
    pinned_containers = [("skybuild-pilot-api", api_container), (DATABASE_CONTAINER, db_container)]
    if gateway_mode:
        pinned_containers.append(("skybuild-workbench", gateway_container))
    for name, identifier in pinned_containers:
        row = json.loads(tls.command("docker", "inspect", name))[0]
        if row.get("Id") != identifier:
            raise ValueError("Pilot container identity changed")
        inspected[name] = row
    api = inspected["skybuild-pilot-api"]
    if api.get("Image") != api_image:
        raise ValueError("Pinned current API image changed")
    ports = {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8000"}]}
    if not gateway_mode:
        ports["8000/tcp"].append({"HostIp": tailnet_ip, "HostPort": "8443"})

    def matching_ports(observed, expected):
        if not isinstance(observed, dict) or set(observed) != set(expected):
            return False
        for port, bindings_expected in expected.items():
            bindings = observed[port]
            if (not isinstance(bindings, list)
                    or any(not isinstance(binding, dict)
                           or set(binding) != {"HostIp", "HostPort"} for binding in bindings)
                    or sorted((b["HostIp"], b["HostPort"]) for b in bindings)
                    != sorted((b["HostIp"], b["HostPort"]) for b in bindings_expected)):
                return False
        return True

    mounts = {(str(state_dir / "tls" / name), "/run/skybuild-tls/" + name, False)
              for name in ("server.crt", "server.key")}
    actual_mounts = {(m.get("Source"), m.get("Destination"), m.get("RW")) for m in api.get("Mounts", [])}
    command = ["python", "-m", "skybuild", "serve", "--host", "0.0.0.0", "--port", "8000",
               "--ssl-certfile", "/run/skybuild-tls/server.crt", "--ssl-keyfile", "/run/skybuild-tls/server.key"]
    host = api.get("HostConfig", {})
    if (api.get("Config", {}).get("User") != str(os.getuid())
            or api.get("Config", {}).get("Cmd") != command
            or not matching_ports(api.get("NetworkSettings", {}).get("Ports"), ports)
            or not matching_ports(host.get("PortBindings"), ports)
            or actual_mounts != mounts or len(api.get("Mounts", [])) != 2
            or not 0 < host.get("Memory", 0) <= 512 * 1024**2
            or not 0 < host.get("PidsLimit", 0) <= 128
            or host.get("RestartPolicy", {}).get("Name") != "unless-stopped"
            or host.get("Privileged") is not False or host.get("NetworkMode") == "host"
            or host.get("PidMode") == "host" or host.get("CapAdd")):
        raise ValueError("Pinned API TLS/bind/user/resource boundary changed")

    gateway_report = {}
    if gateway_mode:
        gateway = inspected["skybuild-workbench"]
        expected_gateway_ports = {"8443/tcp": [
            {"HostIp": "127.0.0.1", "HostPort": "8443"},
            {"HostIp": tailnet_ip, "HostPort": "8443"},
        ]}
        gateway_host = gateway.get("HostConfig", {})
        api_networks = api.get("NetworkSettings", {}).get("Networks", {})
        db_networks = inspected[DATABASE_CONTAINER].get("NetworkSettings", {}).get("Networks", {})
        gateway_networks = gateway.get("NetworkSettings", {}).get("Networks", {})
        if (gateway.get("Image") != gateway_image or gateway.get("State", {}).get("Running") is not True
                or gateway.get("Config", {}).get("User") != str(os.getuid())
                or not matching_ports(gateway.get("NetworkSettings", {}).get("Ports"), expected_gateway_ports)
                or not matching_ports(gateway_host.get("PortBindings"), expected_gateway_ports)
                or len(api_networks) != 1 or set(gateway_networks) != set(api_networks)
                or set(db_networks) != set(api_networks)):
            raise ValueError("Pinned Workbench gateway image, state, bind, or shared network changed")
        network_name, api_network = next(iter(api_networks.items()))
        if (gateway_networks[network_name].get("NetworkID") != api_network.get("NetworkID")
                or db_networks[network_name].get("NetworkID") != api_network.get("NetworkID")
                or not re.fullmatch(r"[0-9a-f]{64}", str(api_network.get("NetworkID", "")))
                or "api" not in api_network.get("Aliases", [])):
            raise ValueError("API, PostgreSQL, and Workbench no longer share the pinned backend network")
        labels = gateway.get("Config", {}).get("Labels", {}) or {}
        if (labels.get("com.docker.compose.project") != "skybuild-pilot"
                or labels.get("com.docker.compose.service") != "workbench"
                or labels.get("com.docker.compose.project.working_dir") != str(gateway_compose_owner_path)):
            raise ValueError("Workbench ownership labels changed")
        expected_gateway_env = {
            "PATH": "/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
        }
        env_rows = gateway.get("Config", {}).get("Env", [])
        observed_gateway_env = {}
        if not isinstance(env_rows, list):
            raise ValueError("Workbench environment boundary changed")
        for item in env_rows:
            if not isinstance(item, str) or "=" not in item:
                raise ValueError("Workbench environment boundary changed")
            key, value = item.split("=", 1)
            if key in observed_gateway_env:
                raise ValueError("Workbench environment boundary changed")
            observed_gateway_env[key] = value
        if observed_gateway_env != expected_gateway_env or gateway_host.get("Tmpfs") not in (None, {}):
            raise ValueError("Workbench environment or temporary filesystem boundary changed")
        gateway_command = gateway.get("Config", {}).get("Cmd", [])
        expected_gateway_command = [
            "--ui-checkout", "/runtime-ui/ui", "--api-checkout", "/app",
            "--ca-file", "/tls/ca.crt", "--backend-hostname", hostname,
            "--backend-connect-host", "api", "--backend-port", "8000",
            "--workbench-token-file", "/run/secrets/skybuild-workbench-token",
            "--workbench-project", "skybuild", "--workbench-username", "user1",
            "--workbench-password-file", "/run/secrets/skybuild-workbench-password",
            "--host", "0.0.0.0", "--container-listener", "--port", "8443",
            "--ssl-certfile", "/tls/server.crt", "--ssl-keyfile", "/tls/server.key",
        ]
        if (gateway.get("Config", {}).get("Entrypoint") != ["python", "/runtime-ui/runtime_ui.py"]
                or gateway_command != expected_gateway_command
                or gateway_host.get("Memory") != 256 * 1024**2
                or gateway_host.get("PidsLimit") != 64
                or gateway_host.get("RestartPolicy", {}).get("Name") != "unless-stopped"
                or gateway_host.get("Privileged") is not False or gateway_host.get("CapAdd")
                or gateway_host.get("NetworkMode") != network_name or gateway_host.get("PidMode") == "host"
                or gateway_host.get("ReadonlyRootfs") is not True):
            raise ValueError("Workbench command, user, resource, or isolation boundary changed")
        expected_gateway_mounts = {
            (str(state_dir / "secrets/workbench-gateway-token"), "/run/secrets/skybuild-workbench-token", False),
            (str(state_dir / "secrets/workbench-user1-password"), "/run/secrets/skybuild-workbench-password", False),
            (str(state_dir / "tls/ca.crt"), "/tls/ca.crt", False),
            (str(state_dir / "tls/server.crt"), "/tls/server.crt", False),
            (str(state_dir / "tls/server.key"), "/tls/server.key", False),
        }
        actual_gateway_mounts = {(mount.get("Source"), mount.get("Destination"), mount.get("RW"))
                                 for mount in gateway.get("Mounts", [])}
        if actual_gateway_mounts != expected_gateway_mounts or len(gateway.get("Mounts", [])) != 5:
            raise ValueError("Workbench secret/TLS mount boundary changed")
        for path in (state_dir / "tls/ca.crt", state_dir / "tls/server.crt", state_dir / "tls/server.key"):
            tls.private_file(path)
        provisioner._read_secret(state_dir / "secrets/workbench-gateway-token", mode=0o600)
        provisioner._read_secret(state_dir / "secrets/workbench-user1-password", mode=0o600)
        gateway_ready = json.loads(tls.command("curl", "--fail", "--silent", "--show-error", "--max-time", "10",
            "--cacert", str(state_dir / "tls/ca.crt"), "--resolve", f"{hostname}:8443:{tailnet_ip}",
            f"https://{hostname}:8443/health/ready"))
        if gateway_ready != {"status": "ready"}:
            raise ValueError("Pinned Workbench TLS gateway is not ready")
        gateway_report = {"gateway_container": gateway_container, "gateway_image": gateway_image,
                          "gateway_ready": gateway_ready,
                          "api_gateway_topology": "loopback API 8000 + pinned Workbench 8443"}
    # Read installed package bytes, not /app/src or a mutable image tag. Output
    # contains code digests only and is bounded to 200 reviewed package files.
    probe = ("import hashlib,json,pathlib,skybuild; p=pathlib.Path(skybuild.__file__).parent; "
             "files=sorted(f for f in p.rglob('*') if f.is_file() and not (f.parent.name=='__pycache__' and f.suffix=='.pyc')); "
             "assert len(files)<=200 and all(f.stat().st_size<=1048576 for f in files); "
             "print(json.dumps({str(f.relative_to(p)):hashlib.sha256(f.read_bytes()).hexdigest() for f in files}))")
    installed = json.loads(tls.command("docker", "exec", "skybuild-pilot-api", "python", "-c", probe))
    if installed != current:
        raise ValueError("Installed API package differs from retained source revision")
    readiness = json.loads(tls.command("curl", "--fail", "--silent", "--show-error", "--max-time", "10",
                                       "--cacert", str(state_dir / "tls/ca.crt"),
                                       "--resolve", f"{hostname}:8000:127.0.0.1",
                                       f"https://{hostname}:8000/health/ready"))
    if readiness != {"status": "ready"}:
        raise ValueError("Pinned current TLS API is not ready")
    if provisioner._dedicated_container(state_dir) != system_id:
        raise ValueError("Dedicated database cluster identity changed")
    secret_dir = state_dir / "secrets"
    tls.private_directory(secret_dir)
    runtime_password = provisioner._read_secret(secret_dir / "runtime-password", mode=0o600)
    expected_env = (f"SKYBUILD_DSN={provisioner._dsn(runtime_password, provisioner.DATABASE, host='db', port=5432)}\n"
                    f"SKYBUILD_EXPECTED_DATABASE={provisioner.DATABASE}\n")
    provisioner._read_secret(secret_dir / "pilot_owner-token", mode=0o600)
    tls.private_file(state_dir / "runtime.env")
    if (state_dir / "runtime.env").read_text() != expected_env:
        raise ValueError("Private runtime environment differs from the scoped pilot role")
    environment = dict(item.split('=', 1) for item in api.get("Config", {}).get("Env", []) if '=' in item)
    expected_values = dict(item.split('=', 1) for item in expected_env.splitlines())
    if ({key: value for key, value in environment.items() if key.startswith('SKYBUILD_')} != expected_values):
        raise ValueError("API environment differs from the restricted runtime configuration")
    password = provisioner._read_secret(secret_dir / "admin-password", mode=0o644)
    dsn = make_conninfo(provisioner._dsn(password, 'postgres', host='127.0.0.1', port=55432),
                        dbname=provisioner.DATABASE)
    with psycopg.connect(dsn, connect_timeout=5,
                         options='-c default_transaction_read_only=on -c statement_timeout=10000 -c lock_timeout=5000') as connection:
        connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        provisioner._require_same_cluster(connection, system_id)
        if connection.execute('SELECT current_database()').fetchone()[0] != provisioner.DATABASE:
            raise ValueError("Promotion database identity differs")
        applied = connection.execute('SELECT version, digest FROM skybuild.schema_migrations ORDER BY version').fetchall()
        expected = [(int(Path(name).name.split('_', 1)[0]), old_migrations[name]) for name in old_names]
        if applied != expected:
            raise ValueError(f"Applied schema is not the exact retained {current_version:03} prefix")
        audit = audit_runtime_role(connection, provisioner.DATABASE, provisioner.ROLE)
        if audit['findings']:
            raise ValueError("Current restricted-role policy is not fully qualified")
    return {'ready_for_operator_promotion': True, 'no_changes_made': True,
            'candidate_source': expected_sha, 'current_source': current_sha,
            'api_container': api_container, 'database_container': db_container, 'api_image': api_image,
            **gateway_report,
            'database': provisioner.DATABASE, 'database_system_id': system_id,
            'current_schema': current_version, 'candidate_schema': candidate_version,
            'schema_transition': schema_transition, 'published_ref': published_ref,
            'current_role_audit': 'passed',
            'candidate_role_audit_required': True, 'tls': tls_report,
            'ca_pem_sha256': ca_pem_sha256,
            'binary_rollback_after_migration': False,
            'next_action': 'Await explicit promotion authority; migrate and requalify transactionally before new API startup'}

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--published-ref", required=True, help="Exact approved refs/heads/dev-NNN or refs/heads/main")
    parser.add_argument("--promotion", action="store_true", help="Inspect the retained controller; never apply changes")
    parser.add_argument("--schema-transition", choices=tuple(SCHEMA_TRANSITIONS), default="011-to-012")
    parser.add_argument("--current-sha")
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--hostname")
    parser.add_argument("--tailnet-ip")
    parser.add_argument("--api-container-id")
    parser.add_argument("--db-container-id")
    parser.add_argument("--api-image-id")
    parser.add_argument("--gateway-container-id")
    parser.add_argument("--gateway-image-id")
    parser.add_argument("--compose-owner-path", type=Path)
    parser.add_argument("--gateway-compose-owner-path", type=Path)
    parser.add_argument("--database-system-id")
    parser.add_argument("--ca-pem-sha256")
    args = parser.parse_args(argv)
    try:
        if args.promotion:
            required = (args.current_sha, args.state_dir, args.hostname, args.tailnet_ip,
                        args.api_container_id, args.db_container_id, args.api_image_id, args.database_system_id,
                        args.ca_pem_sha256)
            if not all(required):
                raise ValueError("Promotion requires retained explicit identities")
            gateway_values = (args.gateway_container_id, args.gateway_image_id, args.compose_owner_path,
                              args.gateway_compose_owner_path)
            if any(value is not None for value in gateway_values) and not all(
                    value is not None for value in gateway_values):
                raise ValueError("Gateway promotion requires container, image, API Compose owner, and gateway Compose owner pins")
            report = promotion_preflight(args.checkout, args.expected_sha, args.published_ref,
                                         current_sha=args.current_sha, state_dir=args.state_dir,
                                         hostname=args.hostname, tailnet_ip=args.tailnet_ip,
                                         api_container=args.api_container_id, db_container=args.db_container_id,
                                         api_image=args.api_image_id, system_id=args.database_system_id,
                                         ca_pem_sha256=args.ca_pem_sha256,
                                         gateway_container=args.gateway_container_id,
                                         gateway_image=args.gateway_image_id,
                                         compose_owner_path=args.compose_owner_path,
                                         gateway_compose_owner_path=args.gateway_compose_owner_path,
                                         schema_transition=args.schema_transition)
        else:
            report = preflight(args.checkout, args.expected_sha, args.published_ref)
    except Exception:
        report = {"ready_for_operator_setup": False, "ready_for_operator_promotion": False,
                  "checks": {}, "no_changes_made": True,
                  "error": "Preflight unavailable; inspect local dependencies"}
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0 if report.get("ready_for_operator_promotion", report.get("ready_for_operator_setup", False)) else 2


if __name__ == "__main__":
    sys.exit(main())
