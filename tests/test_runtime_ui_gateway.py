from pathlib import Path
import ssl
import importlib.util
import certifi
import shutil

import httpx
from fastapi.testclient import TestClient
import pytest

ROOT = Path(__file__).resolve().parents[1] / "ops/runtime-ui"
spec = importlib.util.spec_from_file_location("runtime_ui_under_test", ROOT / "runtime_ui.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
create_app, MAX_REQUEST = module.create_app, module.MAX_REQUEST
UI = ROOT / "ui"
API = ROOT / "api-source"
CA = Path(certifi.where())
HOST = "jeltz.tail991ac1.ts.net"


def app_for(handler):
    return create_app(ui_checkout=UI, api_checkout=API, ca_file=CA,
                      backend_hostname=HOST, backend_connect_host="api",
                      transport=httpx.MockTransport(handler))


def test_live_ui_routes_and_empty_list_have_no_fake_fallback():
    with TestClient(app_for(lambda request: httpx.Response(200, json=[])),
                    base_url="https://localhost:8443") as client:
        home = client.get("/workbench")
        assert home.status_code == 200
        assert 'data-skybuild-preview="true"' not in home.text
        assert "Task List" in home.text and "Live Petri workflow" in home.text
        script = client.get("/workbench/assets/tasks.js").text
        assert "tasks = result;" in script
        assert "result.length === 0 ? fakeTasks" not in script
        assert "localStorage.setItem" not in script
        assert "sessionStorage" not in script
        assert client.get("/workbench/workflow").status_code == 200
        assert "Live" not in client.get("/workbench/assets/live-workflow.js").headers.get("Content-Type", "")


def test_exact_auth_concurrency_idempotency_and_target_forwarding():
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(409, json={"detail": "revision conflict"},
                              headers={"ETag": "7", "Set-Cookie": "secret=not-forwarded"})
    with TestClient(app_for(handler), base_url="https://localhost:8443") as client:
        response = client.post("/api/v1/projects/skybuild/tasks/T/actions?x=%2F",
                               content=b'{"event":"ready"}', headers={
                                   "Authorization": "Bearer test-only-token",
                                   "If-Match": "7", "Idempotency-Key": "stable-key",
                                   "Origin": "https://localhost:8443",
                                   "Cookie": "do-not-forward=secret",
                                   "X-Forwarded-Host": "attacker.invalid"})
    assert response.status_code == 409 and response.headers["etag"] == "7"
    assert "set-cookie" not in response.headers
    upstream = seen[0]
    assert upstream.url.host == "api" and upstream.url.port == 8000
    assert upstream.url.raw_path.endswith(b"?x=%2F")
    assert upstream.extensions["sni_hostname"] == HOST
    assert upstream.headers["host"] == HOST + ":8000"
    assert upstream.headers["authorization"] == "Bearer test-only-token"
    assert upstream.headers["if-match"] == "7"
    assert upstream.headers["idempotency-key"] == "stable-key"
    assert "cookie" not in upstream.headers and "x-forwarded-host" not in upstream.headers
    assert upstream.content == b'{"event":"ready"}'


def test_backend_failure_is_sanitized_and_not_redirected():
    def handler(request):
        raise httpx.ConnectError("Bearer secret-value https://user:password@host", request=request)
    with TestClient(app_for(handler), base_url="https://localhost") as client:
        response = client.get("/api/v1/projects/skybuild/tasks")
        assert response.status_code == 502
        assert response.json() == {"detail": "SkyBuild API unavailable"}
    with TestClient(app_for(lambda request: httpx.Response(302, headers={"Location": "https://attacker.invalid"})),
                    base_url="https://localhost") as client:
        response = client.get("/api/v1/projects/skybuild/tasks", follow_redirects=False)
        assert response.status_code == 302 and "location" not in response.headers


@pytest.mark.parametrize("path", ["/workbench/dev/tasks-preview.json", "/workbench/api/state",
                                 "/workbench/milestones", "/workbench/views/all",
                                 "/workbench/api/marshalls/dunsel"])
def test_unwired_panels_do_not_report_sample_data(path):
    with TestClient(app_for(lambda request: pytest.fail("must not call backend")),
                    base_url="https://localhost") as client:
        response = client.get(path)
        assert response.status_code == 503
        assert "sample state" not in response.text.lower()
        assert "FAKE-" not in response.text


def test_refuses_cross_origin_oversized_body_and_untrusted_host():
    with TestClient(app_for(lambda request: pytest.fail("must not call backend")),
                    base_url="https://localhost") as client:
        assert client.post("/api/v1/projects/skybuild/tasks", headers={"Origin": "https://evil.invalid"}).status_code == 403
        assert client.post("/api/v1/projects/skybuild/tasks", content=b"x" * (MAX_REQUEST + 1)).status_code == 413
        assert client.get("/api/v1/projects/skybuild/tasks", headers={"Host": "evil.invalid"}).status_code == 400
        assert client.get("/workbench/assets/../../runtime_ui.py").status_code != 200


def test_explicit_installation_trust_and_tls_hostname(monkeypatch):
    captured = {}
    original = httpx.AsyncClient
    def capture(**kwargs):
        captured.update(kwargs)
        return original(**kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", capture)
    app = app_for(lambda request: httpx.Response(200, json={}))
    context = captured["verify"]
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    assert captured["trust_env"] is False and captured["follow_redirects"] is False
    assert context.get_ca_certs(binary_form=True)
    with TestClient(app, base_url="https://localhost") as client:
        assert client.get("/health/ready").status_code == 200


def test_changed_frozen_ui_refuses_startup(tmp_path):
    copied = tmp_path / "ui"
    shutil.copytree(UI, copied)
    source = copied / "src/skybuild/static/tasks.js"
    source.write_text(source.read_text() + "\n// unreviewed change\n")
    with pytest.raises(ValueError, match="differs from the reviewed snapshot"):
        create_app(ui_checkout=copied, api_checkout=API, ca_file=CA,
                   backend_hostname=HOST, transport=httpx.MockTransport(lambda request: httpx.Response(200)))


def test_oversized_upstream_response_is_not_forwarded():
    with TestClient(app_for(lambda request: httpx.Response(200, content=b"x" * (module.MAX_RESPONSE + 1))),
                    base_url="https://localhost") as client:
        response = client.get("/api/v1/projects/skybuild/tasks")
        assert response.status_code == 502
        assert response.json() == {"detail": "API response too large"}
