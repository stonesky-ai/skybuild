"""Real-browser coverage for the public workbench and its embedded Ideas page."""
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


def test_sidebar_navigation_loads_and_runs_the_ideas_page(page: Page, workbench_url: str):
    page_errors = []
    page.on("pageerror", lambda error: page_errors.append(error))

    response = page.goto(workbench_url + "/workbench", wait_until="networkidle")
    assert response is not None and response.status == 200
    expect(page.get_by_role("heading", name="Task workbench")).to_be_visible()
    navigation = page.get_by_role("navigation", name="SkyBuild sections")
    expect(navigation.get_by_role("link", name="Home workbench")).to_have_attribute("aria-current", "page")
    assert navigation.evaluate("element => getComputedStyle(element).position") == "fixed"

    with page.expect_response(lambda result: result.url.endswith("/ideas/api/state")) as state_response:
        navigation.get_by_role("link", name="Ideas · SkyKeep tools").click()
    assert state_response.value.status == 200
    expect(page).to_have_url(workbench_url + "/ideas")
    expect(page.locator("#view-title")).to_have_text("Everything")
    expect(page.locator("#loading")).to_have_count(0)
    expect(page.locator("#boxes h2").filter(has_text="Preview")).to_be_visible()
    expect(page.get_by_text("Live SkyKeep data and actions are not connected yet.")).to_be_visible()
    expect(navigation.get_by_role("link", name="Ideas · SkyKeep tools")).to_have_attribute("aria-current", "page")

    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

    navigation.get_by_role("link", name="Home workbench").click()
    expect(page).to_have_url(workbench_url + "/workbench")
    expect(page.get_by_role("heading", name="Task workbench")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert page_errors == []
