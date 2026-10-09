"""A worker preflight sees its own narrow identity without sending a task."""

import json

import httpx
import pytest

from skybuild.fleet_preflight import PreflightError, probe_private_api


URL = "https://jeltz.tail991ac1.ts.net"
TOKEN = "worker-secret-material-for-a-disposable-test"


def checked_probe(*args, **kwargs):
    return probe_private_api(*args, resolve=lambda host: ["100.95.249.118"], **kwargs)


@pytest.fixture
def token_file(tmp_path):
    path = tmp_path / "worker.token"
    path.write_text(TOKEN + "\n")
    path.chmod(0o600)
    return path


def transport_for(identity, *, ready=True):
    def handle(request):
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert request.method == "GET"
        if request.url.path == "/health/ready":
            return httpx.Response(200 if ready else 503, json={"status": "ready" if ready else "unavailable"})
        if request.url.path == "/api/v1/me":
            return httpx.Response(200, json=identity)
        if request.url.path == "/api/v1/projects/skybuild/cord/inbox":
            return httpx.Response(200, json=[])
        raise AssertionError("Unexpected request")
    return httpx.MockTransport(handle)


def identity(**overrides):
    return {"principal_id": "wonko-worker", "is_admin": False,
            "grants": {"skybuild": ["cord:read", "cord:send", "cord:handle"]}} | overrides


def test_private_ready_project_scoped_worker_can_read_inbox(token_file):
    result = checked_probe(URL, "skybuild", token_file, "wonko-worker",
                               transport=transport_for(identity()))
    assert result == {"ready": True, "project_id": "skybuild", "principal_id": "wonko-worker",
                      "scopes": ["cord:handle", "cord:read", "cord:send"],
                      "inbox_access": True, "host": "jeltz.tail991ac1.ts.net"}
    assert TOKEN not in json.dumps(result)


@pytest.mark.parametrize("overrides", [
    {"principal_id": "another-worker"},
    {"is_admin": True},
    {"grants": {"skybuild": ["cord:read", "cord:send", "cord:handle", "tasks:write"]}},
    {"grants": {"skybuild": ["cord:read", "cord:send"]}},
    {"grants": {"skybuild": ["cord:read", "cord:send", "cord:handle"], "other": ["tasks:read"]}},
    {"grants": {"skybuild": [{}]}},
])
def test_wrong_identity_or_broad_or_missing_grants_refused(token_file, overrides):
    with pytest.raises(PreflightError):
        checked_probe(URL, "skybuild", token_file, "wonko-worker",
                          transport=transport_for(identity(**overrides)))


def test_not_ready_or_public_url_or_unsafe_token_refused(token_file):
    with pytest.raises(PreflightError):
        checked_probe(URL, "skybuild", token_file, "wonko-worker",
                          transport=transport_for(identity(), ready=False))
    with pytest.raises(PreflightError, match="Tailscale"):
        checked_probe("https://example.com", "skybuild", token_file, "wonko-worker")
    token_file.chmod(0o644)
    with pytest.raises(PreflightError, match="private regular file"):
        checked_probe(URL, "skybuild", token_file, "wonko-worker")


def test_public_dns_target_refused_before_token_use(token_file):
    with pytest.raises(PreflightError, match="Tailscale addresses"):
        probe_private_api(URL, "skybuild", token_file, "wonko-worker",
                          resolve=lambda host: ["93.184.215.14"])


def test_token_symlink_refused(token_file, tmp_path):
    link = tmp_path / "link.token"
    link.symlink_to(token_file)
    with pytest.raises(PreflightError, match="cannot be read safely"):
        checked_probe(URL, "skybuild", link, "wonko-worker")
