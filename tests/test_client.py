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


def test_cli_cord_send_reads_utf8_file_and_preserves_key(tmp_path, monkeypatch, capsys):
    from skybuild.__main__ import main
    monkeypatch.setenv("SKYBUILD_API_URL", "https://skybuild.test")
    monkeypatch.setenv("SKYBUILD_TOKEN", "secret-token")
    payload = {"recipient": "worker", "subject": "Assignment", "body": "First\n\nCafé `$(echo literal)`"}
    file = tmp_path / "message.json"
    file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    calls = []

    def send(self, project_id, body, *, idempotency_key=None):
        calls.append((project_id, body, idempotency_key))
        return {"message_id": "message-1"}

    monkeypatch.setattr(Client, "send_message", send)
    assert main(["cord-send", "skybuild", "--body-file", str(file), "--idempotency-key", "assignment-1"]) == 0
    assert calls == [("skybuild", payload, "assignment-1")]
    assert json.loads(capsys.readouterr().out) == {"message_id": "message-1"}


def test_cli_cord_inbox_and_actions(monkeypatch, capsys):
    import io
    from skybuild.__main__ import main
    monkeypatch.setenv("SKYBUILD_API_URL", "https://skybuild.test")
    monkeypatch.setenv("SKYBUILD_TOKEN", "secret-token")
    calls = []

    def inbox(self, project_id, *, limit, offset):
        calls.append(("inbox", project_id, limit, offset))
        return [{"message_id": "message-1"}]

    def action(self, project_id, message_id, action, body=None, *, idempotency_key=None):
        calls.append((action, project_id, message_id, body, idempotency_key))
        return {"state": action}

    monkeypatch.setattr(Client, "inbox", inbox)
    monkeypatch.setattr(Client, "message_action", action)
    assert main(["cord-inbox", "skybuild", "--limit", "3", "--offset", "2"]) == 0
    assert main(["cord-receipt", "skybuild", "message-1", "--idempotency-key", "receipt-1"]) == 0
    assert main(["cord-handle", "skybuild", "message-1", "--idempotency-key", "handle-1"]) == 0
    monkeypatch.setattr("sys.stdin", io.StringIO('{"subject":"Result","body":"done\\n","handle_original":true}'))
    assert main(["cord-reply", "skybuild", "message-1", "--body-stdin",
                 "--idempotency-key", "reply-1"]) == 0
    assert calls == [
        ("inbox", "skybuild", 3, 2),
        ("receipt", "skybuild", "message-1", None, "receipt-1"),
        ("handle", "skybuild", "message-1", None, "handle-1"),
        ("reply", "skybuild", "message-1", {"subject": "Result", "body": "done\n",
                                          "handle_original": True}, "reply-1"),
    ]
    output = capsys.readouterr().out
    assert '"message_id": "message-1"' in output
    assert output.count('"state"') == 3


def test_cli_cord_rejects_non_object_and_oversized_payload(tmp_path, monkeypatch, capsys):
    from skybuild.__main__ import main
    monkeypatch.setenv("SKYBUILD_API_URL", "https://skybuild.test")
    monkeypatch.setenv("SKYBUILD_TOKEN", "secret-token")
    file = tmp_path / "message.json"
    calls = []
    monkeypatch.setattr(Client, "send_message", lambda *args, **kwargs: calls.append(args))
    file.write_text('["not an object"]')
    assert main(["cord-send", "skybuild", "--body-file", str(file)]) == 1
    file.write_text("x" * 262_145)
    assert main(["cord-send", "skybuild", "--body-file", str(file)]) == 1
    assert calls == []
    assert "not an object" not in capsys.readouterr().err


def test_cli_reconcile_due_is_bounded_and_reports_continuation(monkeypatch, capsys):
    from skybuild.__main__ import main
    monkeypatch.setenv("SKYBUILD_API_URL", "https://skybuild.test")
    monkeypatch.setenv("SKYBUILD_TOKEN", "secret-token")
    calls = []

    def page(self, project_id, *, limit, after_task_id):
        calls.append((project_id, limit, after_task_id))
        return {"scanned": 2, "reassessed": [f"due-{after_task_id}"],
                "next_after_task_id": "task-m" if after_task_id == "task-k" else "task-o"}

    monkeypatch.setattr(Client, "reconcile_due_deferrals", page)
    assert main(["reconcile-due", "project", "--page-size", "2", "--max-pages", "2", "--after-task-id", "task-k"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert calls == [("project", 2, "task-k"), ("project", 2, "task-m")]
    assert result == {"scanned": 4, "reassessed": ["due-task-k", "due-task-m"],
                      "complete": False, "next_after_task_id": "task-o"}
    assert main(["reconcile-due", "project", "--max-pages", "0"]) == 1
    assert len(calls) == 2


def test_cli_reconcile_due_stops_after_final_page(monkeypatch, capsys):
    from skybuild.__main__ import main
    monkeypatch.setenv("SKYBUILD_API_URL", "https://skybuild.test")
    monkeypatch.setenv("SKYBUILD_TOKEN", "secret-token")
    calls = []

    def page(self, project_id, *, limit, after_task_id):
        calls.append(after_task_id)
        return {"scanned": 1, "reassessed": ["due"], "next_after_task_id": None}

    monkeypatch.setattr(Client, "reconcile_due_deferrals", page)
    assert main(["reconcile-due", "project"]) == 0
    assert calls == [None]
    assert json.loads(capsys.readouterr().out)["complete"] is True


def test_cli_reconcile_due_reports_confirmed_pages_after_later_failure(monkeypatch, capsys):
    from skybuild.__main__ import main
    monkeypatch.setenv("SKYBUILD_API_URL", "https://skybuild.test")
    monkeypatch.setenv("SKYBUILD_TOKEN", "secret-token")

    def page(self, project_id, *, limit, after_task_id):
        if after_task_id:
            raise ClientError("unavailable", "secret-token")
        return {"scanned": limit, "reassessed": ["first"], "next_after_task_id": "task-b"}

    monkeypatch.setattr(Client, "reconcile_due_deferrals", page)
    assert main(["reconcile-due", "project", "--page-size", "2"]) == 1
    output = capsys.readouterr()
    assert json.loads(output.out) == {"scanned": 2, "reassessed": ["first"],
                                     "complete": False, "next_after_task_id": "task-b", "uncertain_page": True}
    assert "secret-token" not in output.out + output.err


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


@pytest.mark.parametrize("retries", [True, False, 1.0, 0.5, "2", None, [], {}, -1, 6])
def test_invalid_retries_fail_before_http_client_creation(monkeypatch, retries):
    def unexpected_client(*args, **kwargs):
        pytest.fail("Invalid retries must fail before creating an HTTP client")
    monkeypatch.setattr("skybuild.client.httpx.Client", unexpected_client)
    with pytest.raises(ValueError, match="Retries must be"):
        Client("https://skybuild.test", "secret-token", retries=retries)


@pytest.mark.parametrize("retries", [0, 1, 5])
def test_valid_retry_boundaries_make_expected_attempts(monkeypatch, retries):
    monkeypatch.setattr("skybuild.client.time.sleep", lambda _: None)
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(503, json={"error": {"code": "unavailable"}})
    with Client("https://skybuild.test", "secret-token", retries=retries,
                transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ClientError):
            client.list_tasks("p")
    assert len(requests) == retries + 1
