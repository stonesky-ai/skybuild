import json

import httpx
import pytest

from skybuild.client import Client, ClientError


def test_retry_preserves_generated_key_revision_and_body(monkeypatch):
    monkeypatch.setattr("skybuild.client.time.sleep", lambda _: None)
    requests = []
    def handle(request):
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ReadError("lost response SECRET", request=request)
        if len(requests) == 2:
            return httpx.Response(503, json={"error": {"code": "unavailable"}})
        return httpx.Response(200, json={"revision": 8})
    with Client("https://skybuild.test", "secret-token", transport=httpx.MockTransport(handle)) as client:
        assert client.update_task("project", "TASK-1", {"title": "new"}, expected_revision=7) == {"revision": 8}
    assert len(requests) == 3
    assert len({request.headers["Idempotency-Key"] for request in requests}) == 1
    assert all(request.headers["If-Match"] == "7" for request in requests)
    assert all(json.loads(request.content) == {"title": "new"} for request in requests)


def test_conflict_never_retries_or_rebases(monkeypatch):
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(409, json={"error": {"code": "stale_revision", "message": "Task changed"}})
    with Client("https://skybuild.test", "secret-token", transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ClientError) as error:
            client.update_task("p", "T", {"title": "new"}, expected_revision=3, idempotency_key="original-key")
    assert error.value.status_code == 409
    assert error.value.code == "stale_revision"
    assert len(requests) == 1
    assert requests[0].headers["Idempotency-Key"] == "original-key"
    assert requests[0].headers["If-Match"] == "3"


def test_retry_exhaustion_is_bounded_and_hides_transport_secrets(monkeypatch):
    monkeypatch.setattr("skybuild.client.time.sleep", lambda _: None)
    requests = []
    def handle(request):
        requests.append(request)
        raise httpx.ConnectError("SECRET token and DSN", request=request)
    with Client("https://skybuild.test", "secret-token", retries=1, transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ClientError) as error:
            client.list_tasks("p")
    assert len(requests) == 2
    assert "SECRET" not in str(error.value)


def test_redirect_does_not_forward_credentials():
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(307, headers={"Location": "https://other.test/"}, json={})
    with Client("https://skybuild.test", "secret-token", transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ClientError):
            client.list_tasks("p")
    assert len(requests) == 1


@pytest.mark.parametrize("url", ["file:///tmp/service", "https://user:secret@skybuild.test", "https://skybuild.test/?token=secret", "https://skybuild.test/#secret"])
def test_client_rejects_credential_or_non_service_urls(url):
    with pytest.raises(ValueError):
        Client(url, "secret-token")


def test_client_never_sends_bearer_to_absolute_override():
    def handle(request):
        pytest.fail("An absolute URL must fail before transport")
    with Client("https://skybuild.test", "secret-token", transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ValueError):
            client.request("GET", "https://other.test/")


def test_malformed_service_error_stays_a_client_error():
    def handle(request):
        return httpx.Response(400, json={"error": "unexpected format"})
    with Client("https://skybuild.test", "secret-token", transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ClientError) as error:
            client.list_tasks("p")
    assert error.value.code == "http_error"


def test_opaque_identifiers_are_preserved_and_encoded():
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={})
    with Client("https://skybuild.test", "secret-token", transport=httpx.MockTransport(handle)) as client:
        client.get_task("project:one", "task ?#é ")
        with pytest.raises(ValueError):
            client.get_task("project", "../other")
    assert requests[0].url.raw_path == b"/api/v1/projects/project%3Aone/tasks/task%20%3F%23%C3%A9%20"
    assert len(requests) == 1


def test_cli_credentials_never_appear_in_error_output(monkeypatch, capsys):
    from skybuild.__main__ import main
    monkeypatch.setenv("SKYBUILD_API_URL", "https://skybuild.test")
    monkeypatch.setenv("SKYBUILD_TOKEN", "SECRET-TOKEN")
    def fail(*args, **kwargs):
        raise ClientError("unavailable", "SECRET-TOKEN")
    monkeypatch.setattr(Client, "list_tasks", fail)
    assert main(["tasks", "p"]) == 1
    output = capsys.readouterr()
    assert "SECRET" not in output.err + output.out


def test_cli_ledger_manifest_needs_no_database_or_credentials(monkeypatch, tmp_path, capsys):
    from skybuild.__main__ import main
    for name in ("SKYBUILD_DSN", "SKYBUILD_EXPECTED_DATABASE", "SKYBUILD_TOKEN", "SKYBUILD_API_URL"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "ledger.md"
    original = b"# Ledger\n\n## SKYBUILD-CLI Title\n\n- Status: proposed\n- Brief: read only\n"
    path.write_bytes(original)
    assert main(["ledger-manifest", str(path)]) == 0
    output = capsys.readouterr()
    assert not output.err
    assert json.loads(output.out)["task_ids"] == ["SKYBUILD-CLI"]
    assert path.read_bytes() == original
    assert main(["ledger-manifest", str(tmp_path / "missing.md")]) == 1
    output = capsys.readouterr()
    assert not output.out
    assert "Operation failed" in output.err
