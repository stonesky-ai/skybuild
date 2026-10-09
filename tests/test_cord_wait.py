"""Bounded mailbox waiting preserves durable reads and current authorization."""

import asyncio
import threading
import time

import httpx
import pytest
from starlette.concurrency import run_in_threadpool

from skybuild.api import create_app
from skybuild.client import Client
from skybuild.contracts import DomainError, Principal


class Mailbox:
    def __init__(self):
        self.messages = []
        self.first_read = threading.Event()
        self.reads = 0
        self.valid = True
        self.allowed = True

    def authenticate(self, token):
        if token != "token" or not self.valid:
            raise DomainError("authentication", "Invalid credentials", 401)
        return Principal("worker", False, {"project": frozenset({"cord:read"})})

    def inbox(self, actor, project, *, limit, offset):
        if not self.allowed or project != "project":
            raise DomainError("authorization", "Project operation not permitted", 403)
        self.reads += 1
        self.first_read.set()
        return self.messages[offset:offset + limit]


def run_scenario(scenario):
    async def run():
        store = Mailbox()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(store)),
                                     base_url="http://test", headers={"Authorization": "Bearer token"}) as client:
            await scenario(client, store)
    asyncio.run(run())


PATH = "/api/v1/projects/project/cord/inbox"


def test_arrival_returns_durable_page_and_other_requests_remain_responsive():
    async def scenario(client, store):
        pending = asyncio.create_task(client.get(PATH, params={"wait_seconds": 2, "limit": 1}))
        assert await run_in_threadpool(store.first_read.wait, 2)
        assert not pending.done()
        assert (await asyncio.wait_for(client.get("/health/live"), 0.2)).status_code == 200
        store.messages = [{"message_id": "first"}, {"message_id": "second"}]
        response = await asyncio.wait_for(pending, 1.5)
        assert response.json() == [{"message_id": "first"}]
        # Waiting neither receipts nor handles the durable messages.
        assert (await client.get(PATH)).json() == store.messages
    run_scenario(scenario)


def test_empty_wait_times_out_and_default_is_one_read():
    async def scenario(client, store):
        assert (await client.get(PATH)).json() == []
        assert store.reads == 1
        started = time.monotonic()
        assert (await client.get(PATH, params={"wait_seconds": 1})).json() == []
        assert 0.9 <= time.monotonic() - started < 2
        assert 3 <= store.reads <= 5
    run_scenario(scenario)


def test_slow_database_read_does_not_block_event_loop():
    async def scenario(client, store):
        original = store.inbox
        release = threading.Event()
        def slow_read(*args, **kwargs):
            store.first_read.set()
            assert release.wait(2)
            return original(*args, **kwargs)
        store.inbox = slow_read
        pending = asyncio.create_task(client.get(PATH))
        try:
            assert await run_in_threadpool(store.first_read.wait, 1)
            assert (await asyncio.wait_for(client.get("/health/live"), 0.2)).status_code == 200
        finally:
            release.set()
        assert (await pending).json() == []
    run_scenario(scenario)


@pytest.mark.parametrize("revocation,status", [("valid", 401), ("allowed", 403)])
def test_wait_rechecks_token_and_project_grants(revocation, status):
    async def scenario(client, store):
        pending = asyncio.create_task(client.get(PATH, params={"wait_seconds": 2}))
        assert await run_in_threadpool(store.first_read.wait, 2)
        setattr(store, revocation, False)
        store.messages = [{"message_id": "must-not-leak"}]
        response = await asyncio.wait_for(pending, 1.5)
        assert response.status_code == status
        assert "must-not-leak" not in response.text
    run_scenario(scenario)


def test_invalid_wait_and_initial_auth_scope_denials():
    async def scenario(client, store):
        for value in (-1, 26, "nan", "0.5"):
            assert (await client.get(PATH, params={"wait_seconds": value})).status_code == 422
        assert (await client.get(PATH, headers={"Authorization": "Bearer bad"},
                                 params={"wait_seconds": 25})).status_code == 401
        assert (await client.get(PATH.replace("project/cord", "other/cord"),
                                 params={"wait_seconds": 25})).status_code == 403
        assert store.reads == 0
    run_scenario(scenario)


def test_disconnect_stops_wait_without_more_reads(monkeypatch):
    async def disconnected(self):
        return True
    monkeypatch.setattr("skybuild.api.Request.is_disconnected", disconnected)

    async def scenario(client, store):
        assert (await client.get(PATH, params={"wait_seconds": 25})).json() == []
        assert store.reads == 1
    run_scenario(scenario)


def test_client_wait_sets_request_timeout_without_changing_default():
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=[])
    with Client("https://test", "token", timeout=2, transport=httpx.MockTransport(handle)) as client:
        assert client.inbox("project", wait_seconds=25) == []
        assert client.inbox("project") == []
        for value in (-1, 26, True, 1.5):
            with pytest.raises(ValueError):
                client.inbox("project", wait_seconds=value)
    assert requests[0].url.params["wait_seconds"] == "25"
    assert requests[0].extensions["timeout"]["read"] == 30
    assert requests[0].extensions["timeout"]["connect"] == 2
    assert "wait_seconds" not in requests[1].url.params
    assert requests[1].extensions["timeout"]["read"] == 2


def test_cli_waits_once_and_prints_messages(monkeypatch, capsys):
    from skybuild.__main__ import main
    monkeypatch.setenv("SKYBUILD_API_URL", "https://test")
    monkeypatch.setenv("SKYBUILD_TOKEN", "token")
    calls = []
    def inbox(self, project, **kwargs):
        calls.append((project, kwargs))
        return [{"message_id": "first"}]
    monkeypatch.setattr(Client, "inbox", inbox)
    assert main(["cord-inbox", "project", "--wait-seconds", "25", "--limit", "1"]) == 0
    assert calls == [("project", {"limit": 1, "offset": 0, "wait_seconds": 25})]
    assert '"message_id": "first"' in capsys.readouterr().out
