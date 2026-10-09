"""Real-browser coverage for Workbench navigation and task refresh behavior."""
from __future__ import annotations

import socket
import threading
import time

import pytest
import uvicorn
from fastapi import FastAPI
from playwright.sync_api import Page, expect

from skybuild.web import install_workbench


@pytest.fixture(scope="module")
def workbench_url():
    """Serve the public pages on loopback; no Store or live service is involved."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    app = FastAPI()
    install_workbench(app)
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="critical", access_log=False,
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=2)
        pytest.fail("loopback web app did not start")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        assert not thread.is_alive(), "loopback web app did not stop cleanly"


def test_shared_sidebar_and_task_api_preview(page: Page, workbench_url: str):
    page_errors = []
    page.on("pageerror", lambda error: page_errors.append(error))
    api_payload = {"tasks": []}
    api_requests = []

    def list_tasks(route):
        api_requests.append(route.request)
        route.fulfill(status=200, json=api_payload["tasks"])

    page.route("**/api/v1/projects/demo/tasks?*", list_tasks)

    response = page.goto(workbench_url + "/workbench", wait_until="networkidle")
    assert response is not None and response.status == 200
    expect(page.get_by_role("heading", name="Task workbench")).to_be_visible()
    navigation = page.get_by_role("navigation", name="SkyBuild sections")
    expect(navigation.get_by_role("link", name="Home workbench")).to_have_attribute("aria-current", "page")
    assert navigation.evaluate("element => getComputedStyle(element).position") == "fixed"

    page.locator("#project").fill("demo")
    page.locator("#token").fill("t" * 40)
    with page.expect_response(lambda result: "/api/v1/projects/demo/tasks?" in result.url) as task_response:
        page.get_by_role("button", name="Connect").click()
    assert task_response.value.status == 200
    assert api_requests[-1].method == "GET"
    assert api_requests[-1].headers["authorization"] == "Bearer " + "t" * 40
    expect(page.get_by_text("[FAKE PREVIEW] FAKE-SKYBUILD-TASK-WORKBENCH", exact=False)).to_be_visible()
    expect(page.locator("#task-count")).to_contain_text("local fake preview data")
    expect(page.locator("#task-list button")).to_be_disabled()

    api_payload["tasks"] = [{
        "task_id": "SKYBUILD-REAL-001", "title": "Real API task", "status": "ready",
        "phase": "test", "blocker": "", "next_action": "Review it", "responsible": "lead",
    }]
    page.get_by_role("button", name="Refresh tasks").click()
    expect(page.get_by_text("SKYBUILD-REAL-001: Real API task", exact=False)).to_be_visible()
    expect(page.get_by_text("FAKE-SKYBUILD-TASK-WORKBENCH", exact=False)).to_have_count(0)

    with page.expect_response(lambda result: result.url.endswith("/workbench/api/state")) as state_response:
        navigation.get_by_role("link", name="Home", exact=True).click()
    assert state_response.value.status == 200
    expect(page).to_have_url(workbench_url + "/workbench/views/alarms")
    expect(page.locator("#view-title")).to_have_text("Home")
    expect(page.locator("#loading")).to_have_count(0)
    navigation.get_by_role("link", name="Everything", exact=True).click()
    expect(page).to_have_url(workbench_url + "/workbench/views/all")
    expect(page.locator("#view-title")).to_have_text("Everything")
    expect(page.locator("#boxes h2").filter(has_text="Preview")).to_be_visible()
    expect(page.get_by_text("Live SkyKeep data and actions are not connected yet.")).to_be_visible()
    expect(navigation.get_by_role("link", name="Everything", exact=True)).to_have_attribute("aria-current", "page")
    expect(navigation.get_by_role("link", name="Build line").last).to_have_attribute("href", "/workbench/views/all#section-flow")

    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

    navigation.get_by_role("link", name="Home workbench").click()
    expect(page).to_have_url(workbench_url + "/workbench")
    expect(page.get_by_role("heading", name="Task workbench")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert page_errors == []
