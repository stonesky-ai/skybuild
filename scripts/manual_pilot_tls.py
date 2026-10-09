"""Prepare/check private application TLS without starting or changing services."""

import argparse
import datetime as dt
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile


class TLSError(ValueError):
    """A required local TLS boundary could not be established."""


def command(*args: str) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=15, check=False)
    if result.returncode:
        raise TLSError(f"Local {args[0]} check failed; no service changes made")
    return result.stdout.strip()


def identity(status: dict, hostname: str, tailnet_ip: str) -> None:
    """Bind the requested listener and SAN to the running local Tailnet identity."""
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,250}[a-z0-9])?\.ts\.net", hostname):
        raise TLSError("Require the exact lowercase controller ts.net hostname")
    labels = hostname.split(".")
    if any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
           for label in labels) or len(hostname) > 253:
        raise TLSError("Invalid controller hostname")
    address = ipaddress.ip_address(tailnet_ip)
    if address.version != 4 or address not in ipaddress.ip_network("100.64.0.0/10"):
        raise TLSError("Require a local Tailscale IPv4 address")
    own = status.get("Self", {})
    if (status.get("BackendState") != "Running"
            or own.get("DNSName", "").rstrip(".") != hostname
            or str(address) not in own.get("TailscaleIPs", [])):
        raise TLSError("Hostname/address do not match this running Tailscale controller")


def private_directory(path: Path) -> None:
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise TLSError("Require an owned mode-0700 directory, not a symlink")


def private_file(path: Path) -> None:
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise TLSError("Require owned mode-0600 TLS files, not symlinks")


def controller(checkout: Path, expected_sha: str, state_dir: Path,
               hostname: str, tailnet_ip: str, *,
               expected_api_image: str | None = None) -> None:
    """Require reviewed source and owned pilot containers with pinned images."""
    if os.getuid() == 0:
        raise TLSError("Run TLS preparation as the non-root pilot state owner")
    if not re.fullmatch(r"[0-9a-f]{40}", expected_sha):
        raise TLSError("Require an explicit reviewed 40-character source SHA")
    if expected_api_image is not None and not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_api_image):
        raise TLSError("Require an exact retained API image ID")
    checkout = checkout.absolute()
    if command("git", "-C", str(checkout), "rev-parse", "--show-toplevel") != str(checkout):
        raise TLSError("Require the exact checkout root")
    for options in ((), ("--push",)):
        if command("git", "-C", str(checkout), "remote", "get-url", *options, "origin") != \
                "https://github.com/stonesky-ai/skybuild.git":
            raise TLSError("Require SkyBuild origin fetch and push URLs")
    if (command("git", "-C", str(checkout), "rev-parse", "HEAD") != expected_sha
            or command("git", "-C", str(checkout), "status", "--porcelain",
                       "--untracked-files=all")):
        raise TLSError("Require the clean reviewed checkout")
    published = command("git", "-C", str(checkout), "ls-remote", "origin",
                        "refs/heads/dev-002", "refs/heads/main")
    if not any(row.split("\t")[0] == expected_sha for row in published.splitlines()):
        raise TLSError("Reviewed source is not published at dev-002 or main")
    private_directory(state_dir)
    if not state_dir.is_absolute() or checkout == state_dir or checkout in state_dir.parents:
        raise TLSError("Keep the private pilot state outside the checkout")
    identity(json.loads(command("tailscale", "status", "--json")), hostname, tailnet_ip)
    for name, service in (("skybuild-pilot-api", "api"), ("skybuild-pilot-pg", "db")):
        rows = json.loads(command("docker", "inspect", name))
        if not isinstance(rows, list) or len(rows) != 1:
            raise TLSError("Unknown pilot container")
        row = rows[0]
        labels = row.get("Config", {}).get("Labels", {}) or {}
        if (labels.get("com.docker.compose.project") != "skybuild-pilot"
                or labels.get("com.docker.compose.service") != service
                or labels.get("com.docker.compose.project.working_dir") != str(checkout / "ops/manual-pilot")
                or not row.get("State", {}).get("Running")):
            raise TLSError("Existing pilot container ownership/running state differs")
        image = "postgres:16" if service == "db" else "skybuild-pilot-api:local"
        expected_image = (expected_api_image if service == "api" and expected_api_image is not None
                          else command("docker", "image", "inspect", image, "--format", "{{.Id}}"))
        if row.get("Image") != expected_image:
            raise TLSError("Pilot container differs from the expected local image")
        ports = row.get("NetworkSettings", {}).get("Ports", {})
        if service == "db" and ports.get("5432/tcp") != [{"HostIp": "127.0.0.1", "HostPort": "55432"}]:
            raise TLSError("Database must remain bound only to the owned loopback port")
        target = "/var/lib/postgresql/data" if service == "db" else None
        if target and not any(m.get("Destination") == target
                              and m.get("Source") == str(state_dir / "pgdata")
                              for m in row.get("Mounts", [])):
            raise TLSError("Pilot database state mount differs")


def check(tls_dir: Path, hostname: str) -> dict:
    """Validate private files, leaf identity, chain, key and remaining lifetime."""
    private_directory(tls_dir)
    for name in ("ca.key", "ca.crt", "server.key", "server.crt"):
        private_file(tls_dir / name)
    ca, cert, key = (str(tls_dir / name) for name in ("ca.crt", "server.crt", "server.key"))
    command("openssl", "verify", "-CAfile", ca, "-purpose", "sslserver",
            "-verify_hostname", hostname, cert)
    san = command("openssl", "x509", "-in", cert, "-noout", "-ext", "subjectAltName")
    if san.splitlines()[1:] != [f"    DNS:{hostname}"]:
        raise TLSError("Leaf must contain exactly the controller DNS SAN")
    eku = command("openssl", "x509", "-in", cert, "-noout", "-ext", "extendedKeyUsage")
    if "TLS Web Server Authentication" not in eku or "TLS Web Client Authentication" in eku:
        raise TLSError("Leaf must have serverAuth only")
    public_cert = command("openssl", "x509", "-in", cert, "-pubkey", "-noout")
    public_key = command("openssl", "pkey", "-in", key, "-pubout")
    if public_cert != public_key:
        raise TLSError("Server certificate and private key differ")
    if command("openssl", "x509", "-in", ca, "-pubkey", "-noout") != \
            command("openssl", "pkey", "-in", str(tls_dir / "ca.key"), "-pubout"):
        raise TLSError("CA certificate and private key differ")
    for filename, maximum_days in ((cert, 30), (ca, 365)):
        dates = command("openssl", "x509", "-in", filename, "-noout", "-startdate", "-enddate")
        start, end = [dt.datetime.strptime(line.split("=", 1)[1], "%b %d %H:%M:%S %Y %Z")
                      for line in dates.splitlines()]
        if not dt.timedelta(0) < end - start <= dt.timedelta(days=maximum_days):
            raise TLSError("Certificate validity exceeds the bounded pilot lifetime")
    command("openssl", "x509", "-in", cert, "-noout", "-checkend", "86400")
    command("openssl", "x509", "-in", ca, "-noout", "-checkend", "86400")
    expiry = command("openssl", "x509", "-in", cert, "-noout", "-enddate")
    return {"hostname": hostname, "leaf_expiry": expiry.removeprefix("notAfter="),
            "ca_sha256": command("openssl", "x509", "-in", ca, "-noout", "-fingerprint", "-sha256"),
            "runtime_uid": os.getuid(),
            "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(), "service_changes": False}


def generate(state_dir: Path, hostname: str) -> dict:
    """Create one new isolated CA/leaf set, never overwrite or start a service."""
    private_directory(state_dir)
    destination = state_dir / "tls"
    if destination.exists() or destination.is_symlink():
        raise TLSError("TLS state already exists; check it or plan explicit rotation")
    staging = Path(tempfile.mkdtemp(prefix=".tls-", dir=state_dir))
    old_umask = os.umask(0o077)
    try:
        ca_key, ca, key, request, cert = (str(staging / name) for name in
                                        ("ca.key", "ca.crt", "server.key", "server.csr", "server.crt"))
        command("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256",
                "-days", "365", "-subj", "/CN=SkyBuild private pilot CA", "-keyout", ca_key,
                "-out", ca, "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
                "-addext", "keyUsage=critical,keyCertSign,cRLSign")
        command("openssl", "req", "-new", "-newkey", "rsa:2048", "-nodes", "-sha256",
                "-subj", f"/CN={hostname}", "-keyout", key, "-out", request)
        extensions = staging / "server.ext"
        extensions.write_text("basicConstraints=critical,CA:FALSE\n"
                              "keyUsage=critical,digitalSignature,keyEncipherment\n"
                              "extendedKeyUsage=serverAuth\n"
                              f"subjectAltName=DNS:{hostname}\n")
        command("openssl", "x509", "-req", "-in", request, "-CA", ca, "-CAkey", ca_key,
                "-set_serial", f"0x{os.urandom(16).hex()}", "-days", "30", "-sha256",
                "-extfile", str(extensions), "-out", cert)
        Path(request).unlink()
        extensions.unlink()
        report = check(staging, hostname)
        staging.rename(destination)
        return report
    finally:
        os.umask(old_umask)
        if staging.exists():
            shutil.rmtree(staging)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("generate", "check"))
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--hostname", required=True)
    parser.add_argument("--tailnet-ip", required=True)
    args = parser.parse_args(argv)
    try:
        controller(args.checkout, args.expected_sha, args.state_dir, args.hostname, args.tailnet_ip)
        report = generate(args.state_dir, args.hostname) if args.action == "generate" else \
            check(args.state_dir / "tls", args.hostname)
        report["endpoint"] = f"https://{args.hostname}:8443"
        report["tailnet_ip"] = args.tailnet_ip
        print(json.dumps(report, sort_keys=True))
        return 0
    except (OSError, ValueError, subprocess.TimeoutExpired, IndexError, TypeError, KeyError) as exc:
        error = str(exc) if isinstance(exc, TLSError) else "TLS preparation/check refused; inspect local prerequisites"
        print(json.dumps({"ready": False, "error": error,
                          "service_changes": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
