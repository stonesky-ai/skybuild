from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from skybuild.web import install_workbench
from skybuild.workbench import marshalls


def client(*, preview=True, host="127.0.0.1"):
    app = FastAPI()
    install_workbench(app, dev_reload=preview)
    return TestClient(app, base_url="http://127.0.0.1:8766", client=(host, 1234))


def test_service_does_not_install_local_process_routes():
    with client(preview=False) as session:
        for path in ("/workbench/marshalls", "/workbench/api/marshalls/dunsel",
                     "/workbench/assets/marshalls.js"):
            assert session.get(path).status_code == 404
        assert session.post("/workbench/api/marshalls/dunsel/start").status_code == 404


def test_local_preview_assets_and_read_only_status(monkeypatch):
    monkeypatch.setattr(marshalls, "snapshot", lambda: {"running": False})
    monkeypatch.setattr(marshalls, "start", lambda: pytest.fail("GET must not start a worker"))
    with client() as session:
        assert 'href="/workbench/marshalls"' in session.get("/workbench").text
        assert session.get("/workbench/marshalls").status_code == 200
        assert session.get("/workbench/assets/marshalls.js").status_code == 200
        assert session.get("/workbench/api/marshalls/dunsel").json() == {"running": False}


@pytest.mark.parametrize("origin", [None, "http://evil.example", "http://127.0.0.1:8767",
                                    "https://127.0.0.1:8766", "http://127.0.0.1:bad"])
def test_invalid_origin_cannot_mutate(origin, monkeypatch):
    monkeypatch.setattr(marshalls, "start", lambda: pytest.fail("unauthorized start"))
    with client() as session:
        headers = {} if origin is None else {"Origin": origin}
        assert session.post("/workbench/api/marshalls/dunsel/start", headers=headers).status_code == 403


def test_remote_client_and_untrusted_host_are_refused(monkeypatch):
    monkeypatch.setattr(marshalls, "snapshot", lambda: pytest.fail("unauthorized status"))
    with client(host="192.0.2.1") as session:
        assert session.get("/workbench/api/marshalls/dunsel").status_code == 403
    with client() as session:
        assert session.get("/workbench/api/marshalls/dunsel", headers={"Host": "evil.example"}).status_code == 403


def test_same_origin_explicit_control_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(marshalls, "start", lambda: calls.append("start") or {"started": True, "pid": 123})
    with client() as session:
        response = session.post("/workbench/api/marshalls/dunsel/start", headers={"Origin": "http://127.0.0.1:8766"})
        assert response.status_code == 200
        assert response.json()["started"]
        assert calls == ["start"]
