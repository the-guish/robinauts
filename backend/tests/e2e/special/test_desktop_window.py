# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The desktop's window: ``robinauts desktop`` with Playwright's Chromium as the window,
headless, which oneberth's launcher starts with ``--app`` on a profile in the data directory.
The test reaches the window over Chromium's debugging port. The person lands in the interface
signed in, having typed nothing, and talks to an agent whose turn runs on a worker's process.
Closing the window stops the application, and no process is left."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, BrowserType, Page, expect, sync_playwright

from util.browser import expect_thread, rail, send
from util.desktop import Desktop
from util.fake_openai import FakeLocalGPTServer
from util.stack import API_KEY, ROOT, config_for, free_port

pytestmark = pytest.mark.io

CONNECT_SECONDS = 180.0


@pytest.fixture
def desktop(tmp_path: Path, local_gpt: FakeLocalGPTServer) -> Iterator[Desktop]:
    if not (ROOT / "frontend" / "dist" / "index.html").is_file():
        pytest.fail("the interface is not built: run scripts/check-e2e.sh")
    interface = {**API_KEY, "ROBINAUTS_UI_DIR": str(ROOT / "frontend" / "dist")}
    made = Desktop(tmp_path, config_for(local_gpt.base_url), interface)
    try:
        yield made
    finally:
        made.stop_all()


def connected(chromium: BrowserType, port: int) -> Browser:
    """The window's browser, once its debugging port answers."""
    deadline = time.monotonic() + CONNECT_SECONDS
    while True:
        try:
            return chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        except Exception:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.5)


def the_window(browser: Browser) -> Page:
    deadline = time.monotonic() + CONNECT_SECONDS
    while time.monotonic() < deadline:
        for context in browser.contexts:
            for page in context.pages:
                if "/ui/" in page.url:
                    return page
        time.sleep(0.2)
    raise AssertionError("the window shows no interface")


def test_the_window_opens_signed_in_and_closing_it_stops_the_desktop(desktop: Desktop) -> None:
    port = free_port()
    with sync_playwright() as playwright:
        chromium = playwright.chromium
        launched = desktop.launch(
            chromium.executable_path,
            ["--headless=new", "--no-sandbox", f"--remote-debugging-port={port}"],
        )
        browser = connected(chromium, port)
        page = the_window(browser)
        # The window opens on the interface. The shell then goes to its first application,
        # which adds a fragment: when it has, the address reads /ui/#/robinauts.
        assert page.url.split("#")[0] == f"{desktop.front_url}/ui/"
        expect(rail(page).get_by_role("link", name="Robinauts")).to_have_attribute(
            "aria-current", "page"
        )
        page.get_by_role("button", name="New chat").click()
        page.get_by_label("Agent").select_option("langchain")
        send(page, "hello from the window")
        expect_thread(page, [("hello from the window", "hello from the window")])
        assert (desktop.data / "workers" / "default" / "langchain.db").is_file()
        assert (desktop.data / "window").is_dir()

        # The person closes the window.
        browser.new_browser_cdp_session().send("Browser.close")
    assert desktop.ended(launched) == 0
    assert desktop.left_behind() == []
