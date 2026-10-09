"""Local certificate, identity and deployment-boundary checks; no live services."""

import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import manual_pilot_tls as tls  # noqa: E402


HOST = "controller.tail12345.ts.net"
IP = "100.100.10.20"


def snapshot():
    return {"BackendState": "Running", "Self": {"DNSName": HOST + ".", "TailscaleIPs": [IP]}}


@pytest.mark.parametrize("hostname,ip", [("other.tail12345.ts.net", IP), (HOST, "100.100.10.21"),
                                         (HOST, "0.0.0.0"), (HOST, "127.0.0.1"),
                                         ("a/../../x.ts.net", IP), ("a..ts.net", IP),
                                         (HOST, "fd7a:115c:a1e0::1")])
def test_unknown_or_foreign_identity_refused(hostname, ip):
    with pytest.raises(ValueError):
        tls.identity(snapshot(), hostname, ip)


def test_only_current_controller_identity_allowed():
    tls.identity(snapshot(), HOST, IP)
    with pytest.raises(tls.TLSError):
        tls.identity({**snapshot(), "BackendState": "Stopped"}, HOST, IP)


def test_real_certificates_exact_san_usage_private_and_no_overwrite(tmp_path):
    tmp_path.chmod(0o700)
    result = tls.generate(tmp_path, HOST)
    directory = tmp_path / "tls"
    assert result["hostname"] == HOST
    assert result["service_changes"] is False
    assert "leaf_expiry" in result
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert {p.name for p in directory.iterdir()} == {"ca.key", "ca.crt", "server.key", "server.crt"}
    for path in directory.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # The overlay runs the host owner's numeric UID: the kernel checks that UID
    # on the readonly bind mount, instead of image USER 10001.
    assert result["runtime_uid"] == os.getuid()
    assert (directory / "server.key").stat().st_uid == result["runtime_uid"]
    descriptor = os.open(directory / "server.key", os.O_RDONLY)
    os.close(descriptor)
    original = (directory / "ca.key").read_bytes()
    with pytest.raises(tls.TLSError):
        tls.generate(tmp_path, HOST)
    assert (directory / "ca.key").read_bytes() == original
    assert tls.check(directory, HOST)["hostname"] == HOST
    with pytest.raises(tls.TLSError):
        tls.check(directory, "foreign.tail12345.ts.net")
    (directory / "server.key").chmod(0o644)
    with pytest.raises(tls.TLSError):
        tls.check(directory, HOST)


def test_private_state_symlinks_and_loose_mode_refused(tmp_path):
    tmp_path.chmod(0o755)
    with pytest.raises(tls.TLSError):
        tls.generate(tmp_path, HOST)
    tmp_path.chmod(0o700)
    link = tmp_path / "link"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(tls.TLSError):
        tls.private_directory(link)


def test_failed_openssl_cleans_only_owned_staging(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    untouched = tmp_path / "untouched"
    untouched.write_text("retain")
    monkeypatch.setattr(tls, "command", lambda *args: (_ for _ in ()).throw(tls.TLSError("failed")))
    with pytest.raises(tls.TLSError):
        tls.generate(tmp_path, HOST)
    assert list(tmp_path.iterdir()) == [untouched]


@pytest.mark.parametrize("defect", ["dirty", "unpublished", "foreign-origin", "foreign-container", "foreign-mount"])
def test_controller_guards_before_certificate_generation(tmp_path, monkeypatch, defect):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    sha = "a" * 40

    def command(*args):
        if args[0] == "git":
            if "--show-toplevel" in args:
                return str(checkout)
            if "rev-parse" in args:
                return sha
            if "status" in args:
                return " M source.py" if defect == "dirty" else ""
            if "ls-remote" in args:
                return ("b" * 40 if defect == "unpublished" else sha) + "\trefs/heads/dev-002"
            return "https://foreign/repo.git" if defect == "foreign-origin" else "https://github.com/stonesky-ai/skybuild.git"
        if args[0] == "tailscale":
            return json.dumps(snapshot())
        if args[:3] == ("docker", "image", "inspect"):
            return "sha256:local"
        service = "api" if args[-1] == "skybuild-pilot-api" else "db"
        return json.dumps([{"Config": {"Labels": {
            "com.docker.compose.project": "foreign" if defect == "foreign-container" else "skybuild-pilot",
            "com.docker.compose.service": service,
            "com.docker.compose.project.working_dir": str(checkout / "ops/manual-pilot")}},
            "Image": "sha256:local", "NetworkSettings": {"Ports": {
                "5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "55432"}]}},
            "State": {"Running": True}, "Mounts": [{"Destination": "/var/lib/postgresql/data",
                "Source": "/foreign" if defect == "foreign-mount" else str(state / "pgdata")}]}])

    monkeypatch.setattr(tls, "command", command)
    with pytest.raises(tls.TLSError):
        tls.controller(checkout, sha, state, HOST, IP)
    assert not (state / "tls").exists()


def test_overlay_only_tailnet_bind_leaf_mounts_and_native_flags():
    text = (Path(__file__).parents[1] / "ops/manual-pilot/compose.tls.yaml").read_text()
    assert ':8443:8000"' in text
    assert "SKYBUILD_PILOT_TAILNET_IP:?" in text
    assert "SKYBUILD_PILOT_TLS_UID:?" in text
    assert "--ssl-certfile" in text and "--ssl-keyfile" in text
    assert "server.crt:/run/skybuild-tls/server.crt:ro" in text
    assert "server.key:/run/skybuild-tls/server.key:ro" in text
    assert "ca.key" not in text and "ca.crt" not in text
    assert "0.0.0.0:8443" not in text and "privileged" not in text


def test_rendered_compose_keeps_database_local_and_mounts_only_leaf(tmp_path):
    root = Path(__file__).parents[1]
    environment = {**os.environ, "SKYBUILD_PILOT_STATE": str(tmp_path),
                   "SKYBUILD_PILOT_TAILNET_IP": IP, "SKYBUILD_PILOT_TLS_UID": str(os.getuid())}
    result = subprocess.run(["docker", "compose", "-f", str(root / "ops/manual-pilot/compose.yaml"),
                             "-f", str(root / "ops/manual-pilot/compose.tls.yaml"),
                             "config", "--format", "json"], env=environment,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, "Compose overlay did not render"
    services = json.loads(result.stdout)["services"]
    api = services["api"]
    assert {(p["host_ip"], str(p["published"]), p["target"]) for p in api["ports"]} == {
        ("127.0.0.1", "8000", 8000), (IP, "8443", 8000)}
    assert str(api["user"]) == str(os.getuid())
    assert {(m["target"], m["read_only"]) for m in api["volumes"]} == {
        ("/run/skybuild-tls/server.crt", True), ("/run/skybuild-tls/server.key", True)}
    assert {(p["host_ip"], str(p["published"])) for p in services["db"]["ports"]} == {
        ("127.0.0.1", "55432")}


@pytest.mark.skipif(os.environ.get("SKYBUILD_TLS_CONTAINER_TEST") != "1",
                    reason="Explicit isolated local-image container check only")
def test_effective_container_user_reads_private_readonly_leaf(tmp_path):
    """No service/CA: verify numeric UID readability in the actual local image."""
    key = tmp_path / "server.key"
    key.write_text("disposable readability fixture")
    key.chmod(0o600)
    result = subprocess.run([
        "docker", "run", "--rm", "--pull=never", "--network=none", "--memory=64m",
        "--pids-limit=16", "--user", str(os.getuid()), "--read-only",
        "--mount", f"type=bind,source={key},target=/run/server.key,readonly",
        "--entrypoint", "python", "skybuild-pilot-api:local", "-c",
        "import os; assert os.geteuid() != 0; f=os.open('/run/server.key', os.O_RDONLY); os.close(f)"
    ], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, "Private leaf was not readable by the configured non-root UID"
