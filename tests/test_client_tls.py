"""Real, local, ephemeral TLS handshakes never use operator trust or secrets."""

from contextlib import contextmanager
import hashlib
import socket
import ssl
import subprocess
import threading

import httpx
import pytest

from skybuild.client import Client, ClientError, ca_file_sha256


@pytest.fixture(scope="module")
def certificates(tmp_path_factory):
    directory = tmp_path_factory.mktemp("ephemeral-tls")
    def openssl(*arguments):
        subprocess.run(["openssl", *map(str, arguments)], cwd=directory, check=True,
                       capture_output=True, timeout=10)
    for name in ("ca", "other-ca"):
        openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                "-subj", f"/CN=Ephemeral test {name}", "-keyout", name + ".key",
                "-out", name + ".pem", "-addext", "basicConstraints=critical,CA:TRUE",
                "-addext", "keyUsage=critical,keyCertSign,cRLSign")
    openssl("req", "-new", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=localhost",
            "-keyout", "server.key", "-out", "server.csr")
    (directory / "extensions").write_text(
        "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\nsubjectAltName=DNS:localhost\n")
    openssl("x509", "-req", "-in", "server.csr", "-CA", "ca.pem", "-CAkey", "ca.key",
            "-CAcreateserial", "-days", "1", "-extfile", "extensions", "-out", "server.pem")
    (directory / "index").write_text("")
    (directory / "serial").write_text("1000\n")
    (directory / "ca.conf").write_text(
        "[ca]\ndefault_ca=local\n[local]\ndatabase=index\nserial=serial\n"
        "new_certs_dir=.\ncertificate=ca.pem\nprivate_key=ca.key\ndefault_md=sha256\n"
        "policy=policy\n[policy]\ncommonName=supplied\n")
    openssl("ca", "-batch", "-config", "ca.conf", "-in", "server.csr", "-out", "expired.pem",
            "-startdate", "20000101000000Z", "-enddate", "20000102000000Z", "-extfile", "extensions")
    return directory


@contextmanager
def tls_server(certificates, *, expired=False):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificates / ("expired.pem" if expired else "server.pem"),
                            certificates / "server.key")
    listener = socket.create_server(("127.0.0.1", 0))
    listener.settimeout(2)
    received = []
    failures = []
    def serve():
        try:
            raw, _ = listener.accept()
            with raw:
                raw.settimeout(2)
                with context.wrap_socket(raw, server_side=True) as connection:
                    request = b""
                    while b"\r\n\r\n" not in request and len(request) < 8192:
                        chunk = connection.recv(4096)
                        if not chunk:
                            break
                        request += chunk
                    received.append(request)
                    connection.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n[]')
        except (OSError, ssl.SSLError) as error:
            failures.append(type(error).__name__)
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1], received
    finally:
        listener.close()
        thread.join(timeout=3)
        assert not thread.is_alive()


def test_provided_ca_authenticates_hostname_and_ignores_environment_proxies(certificates, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("SSL_CERT_FILE", str(certificates / "other-ca.pem"))
    with tls_server(certificates) as (port, received):
        with Client(f"https://localhost:{port}", "ephemeral-token", retries=0, timeout=1,
                    ca_file=certificates / "ca.pem") as client:
            assert client.inbox("project") == []
    assert len(received) == 1
    assert b"Authorization: Bearer ephemeral-token" in received[0]


@pytest.mark.parametrize("failure", ["wrong-ca", "wrong-hostname", "expired", "system-trust"])
def test_invalid_tls_never_transmits_bearer(certificates, failure):
    with tls_server(certificates, expired=failure == "expired") as (port, received):
        host = "127.0.0.1" if failure == "wrong-hostname" else "localhost"
        ca = None if failure == "system-trust" else certificates / (
            "other-ca.pem" if failure == "wrong-ca" else "ca.pem")
        with Client(f"https://{host}:{port}", "must-not-cross-tls", retries=0, timeout=1,
                    ca_file=ca, trust_env=False) as client:
            with pytest.raises(ClientError, match="unavailable"):
                client.inbox("project")
    assert received == []


def test_missing_invalid_or_changed_ca_fails_before_transport(certificates, tmp_path):
    def never(_request):
        pytest.fail("Invalid CA must fail before sending credentials")
    transport = httpx.MockTransport(never)
    bad = tmp_path / "invalid.pem"
    bad.write_text("not a certificate")
    for path in (tmp_path / "missing.pem", bad):
        with pytest.raises(ValueError, match="CA file"):
            Client("https://localhost", "secret", ca_file=path, transport=transport)
    with pytest.raises(ValueError, match="durable dispatch intent"):
        Client("https://localhost", "secret", ca_file=certificates / "ca.pem",
               expected_ca_sha256="0" * 64, transport=transport)
    with pytest.raises(ValueError, match="HTTPS"):
        Client("http://localhost", "secret", ca_file=certificates / "ca.pem", transport=transport)
    with pytest.raises(ValueError, match="required"):
        Client("https://localhost", "secret", expected_ca_sha256="0" * 64, transport=transport)


def test_context_contains_only_provided_ca_and_checks_identity(certificates, monkeypatch):
    captured = {}
    def build(**kwargs):
        captured.update(kwargs)
        return object()
    monkeypatch.setattr("skybuild.client.httpx.Client", build)
    path = certificates / "ca.pem"
    Client("https://localhost", "secret", ca_file=path)
    context = captured["verify"]
    assert context.protocol == ssl.PROTOCOL_TLS_CLIENT
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert len(context.get_ca_certs()) == 1
    assert captured["trust_env"] is False
    assert captured["follow_redirects"] is False
    assert ca_file_sha256(path) == hashlib.sha256(path.read_bytes()).hexdigest()


def test_explicit_ca_redirect_does_not_forward_token(certificates):
    requests = []
    def redirect(request):
        requests.append(request)
        return httpx.Response(307, headers={"Location": "https://other.invalid"}, json={})
    with Client("https://localhost", "secret", ca_file=certificates / "ca.pem",
                transport=httpx.MockTransport(redirect)) as client:
        with pytest.raises(ClientError):
            client.inbox("project")
    assert len(requests) == 1


@pytest.mark.parametrize("prefix", [True, False])
def test_shared_cord_cli_passes_ca_before_or_after_command(tmp_path, monkeypatch, capsys, prefix):
    from skybuild.__main__ import main
    captured = []
    class FakeClient:
        def __init__(self, url, token, **kwargs):
            captured.append(kwargs)
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def inbox(self, project, **kwargs):
            return []
    monkeypatch.setenv("SKYBUILD_API_URL", "https://controller.ts.net:8443")
    monkeypatch.setenv("SKYBUILD_TOKEN", "test-token")
    monkeypatch.setattr("skybuild.__main__.Client", FakeClient)
    path = tmp_path / "ca.pem"
    flag = ["--ca-file", str(path)]
    args = flag + ["cord-inbox", "project"] if prefix else ["cord-inbox", "project"] + flag
    assert main(args) == 0
    assert captured == [{"ca_file": path}]
    assert capsys.readouterr().out.strip() == "[]"


@pytest.mark.parametrize("tls", [False, True])
def test_serve_preserves_plain_default_and_passes_paired_ssl_paths(tmp_path, monkeypatch, tls):
    from skybuild.__main__ import main
    import skybuild.store
    import uvicorn
    captured = []
    monkeypatch.setenv("SKYBUILD_DSN", "not-used")
    monkeypatch.setenv("SKYBUILD_EXPECTED_DATABASE", "not-used")
    monkeypatch.setattr(skybuild.store, "Store", lambda *_: object())
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: captured.append(kwargs))
    cert, key = tmp_path / "server.pem", tmp_path / "server.key"
    args = ["serve", "--port", "8443", "--host", "0.0.0.0"]
    if tls:
        args += ["--ssl-certfile", str(cert), "--ssl-keyfile", str(key)]
    assert main(args) == 0
    expected = {"host": "0.0.0.0", "port": 8443}
    if tls:
        expected.update(ssl_certfile=str(cert), ssl_keyfile=str(key))
    assert captured == [expected]


@pytest.mark.parametrize("flag", ["--ssl-certfile", "--ssl-keyfile"])
def test_serve_rejects_unpaired_ssl_before_configuration_or_database(flag, monkeypatch):
    from skybuild.__main__ import main
    monkeypatch.delenv("SKYBUILD_DSN", raising=False)
    with pytest.raises(SystemExit) as error:
        main(["serve", flag, "unused.pem"])
    assert error.value.code == 2


def test_preflight_custom_ca_keeps_tailnet_identity_and_scopes(certificates, tmp_path):
    from skybuild.fleet_preflight import probe_private_api
    token = tmp_path / "worker.token"
    token.write_text("t" * 32)
    token.chmod(0o600)
    def handle(request):
        if request.url.path == "/health/ready":
            return httpx.Response(200, json={"status": "ready"})
        if request.url.path == "/api/v1/me":
            return httpx.Response(200, json={"principal_id": "worker", "is_admin": False,
                "grants": {"project": ["tasks:read", "cord:read", "cord:send", "cord:handle"]}})
        assert request.url.path in {"/api/v1/projects/project/cord/inbox",
                                    "/api/v1/projects/project/tasks"}
        return httpx.Response(200, json=[])
    report = probe_private_api("https://controller.ts.net:8443", "project", token, "worker",
                               ca_file=certificates / "ca.pem", transport=httpx.MockTransport(handle),
                               resolve=lambda _: ["100.100.100.100"])
    assert report["ready"] is True
    assert report["principal_id"] == "worker"
    assert report["host"] == "controller.ts.net"


def test_preflight_cli_passes_ca_file(tmp_path, monkeypatch, capsys):
    from skybuild import fleet_preflight
    captured = []
    monkeypatch.setattr(fleet_preflight, "probe_private_api",
                        lambda *args, **kwargs: captured.append(kwargs) or {"ready": True})
    ca = tmp_path / "ca.pem"
    monkeypatch.setattr("sys.argv", ["preflight", "--url", "https://controller.ts.net:8443", "--project", "project",
                                    "--token-file", "unused", "--principal", "worker", "--ca-file", str(ca)])
    assert fleet_preflight.main() == 0
    assert captured == [{"ca_file": ca}]
    capsys.readouterr()


def test_manual_cord_cli_passes_same_ca_to_preflight_and_client(tmp_path, monkeypatch, capsys):
    from skybuild import manual_cord
    captured = []
    class FakeClient:
        def __init__(self, *args, **kwargs):
            captured.append(("client", kwargs))
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
    monkeypatch.setattr(manual_cord, "Client", FakeClient)
    monkeypatch.setattr(manual_cord, "_token_from_file", lambda _: "unused-test-token")
    monkeypatch.setattr(manual_cord, "probe_private_api",
                        lambda *args, **kwargs: captured.append(("probe", kwargs)))
    monkeypatch.setattr(manual_cord, "receive_assignment", lambda *args, **kwargs: {"received": True})
    ca = tmp_path / "ca.pem"
    assert manual_cord.main(["--url", "https://controller.ts.net:8443", "--project", "project",
        "--token-file", "unused", "--worker", "worker", "--checkout", str(tmp_path), "--ca-file", str(ca),
        "receive", "--dispatcher", "dispatcher", "--message-id", "message", "--destination", "unused"]) == 0
    assert captured == [("probe", {"ca_file": ca}),
                        ("client", {"retries": 0, "trust_env": False, "ca_file": ca})]
    capsys.readouterr()


def test_dispatch_cli_passes_ca_file(tmp_path, monkeypatch, capsys):
    from skybuild import manual_dispatch
    captured = []
    monkeypatch.setattr(manual_dispatch, "dispatch", lambda *args, **kwargs: captured.append(kwargs) or {"status": "sent"})
    ca = tmp_path / "ca.pem"
    monkeypatch.setattr("sys.argv", ["dispatch", "--checkout", str(tmp_path), "--brief-path", "unused",
        "--worker", "worker", "--dispatcher", "dispatcher", "--project", "project", "--principal", "dispatcher",
        "--url", "https://controller.ts.net:8443", "--token-file", "unused", "--state-dir", "unused", "--ca-file", str(ca)])
    assert manual_dispatch.main() == 0
    assert captured[0]["ca_file"] == ca
    capsys.readouterr()
