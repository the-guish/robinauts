# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The right side panel, and an MCP app in it: the to-do list of ``mcp/to-do-list/``, run as
its own command beside the platform, as an operator would run it.

The [+] menu offers the agent's apps. The app's view opens in a sandboxed frame, talks to the
panel, and through it to the server, which keeps what was done in its file.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import FrameLocator, Page, Route, expect

from util.browser import send
from util.fake_openai import FakeModel, ToolEchoModel
from util.stack import ROOT, free_port

pytestmark = [pytest.mark.io, pytest.mark.database]

TO_DO_LIST = ROOT / "mcp" / "to-do-list"
AGENT = "pydantic_ai_tools"
# The id the configuration of ``util.stack`` gives the MCP server, and the app's tool.
SERVER = "tools"
TOOL = "show_todos"
# A first run of the command installs the server's own locked environment.
START_SECONDS = 180


@contextmanager
def to_do_list(data: Path) -> Iterator[str]:
    """The to-do list server's URL, its list kept in ``data``, stopped when the block ends."""
    port = free_port()
    # The backend's environment is not the server's: uv finds the server's own from its folder.
    environment = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    command = [
        *("uv", "run", "--locked", "--directory", str(TO_DO_LIST)),
        *("robinauts-to-do-list", "--port", str(port), "--file", str(data)),
    ]
    with subprocess.Popen(command, env=environment) as running:
        try:
            deadline = time.monotonic() + START_SECONDS
            while True:
                if running.poll() is not None:
                    raise RuntimeError(f"the to-do list exited with {running.returncode}")
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=1).close()
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise RuntimeError("the to-do list did not start") from None
                    time.sleep(0.2)
            yield f"http://127.0.0.1:{port}/mcp"
        finally:
            running.terminate()
            running.wait(timeout=10)


@pytest.fixture
def fake_model() -> FakeModel:
    return ToolEchoModel("add_todo")


@pytest.fixture
def tools_url(tmp_path: Path) -> Iterator[str]:
    with to_do_list(tmp_path / "todos.json") as url:
        yield url


def menu(page: Page) -> list[str]:
    """Open "Add a tab" and read what it offers."""
    page.get_by_role("button", name="Add a tab").click()
    items = page.get_by_role("menu", name="Tabs to add").get_by_role("menuitem")
    expect(items.first).to_be_visible()
    return items.all_text_contents()


def pick(page: Page, title: str) -> None:
    page.get_by_role("button", name="Add a tab").click()
    page.get_by_role("menuitem", name=title).click()


def view(page: Page) -> FrameLocator:
    return page.frame_locator('iframe[title="To-do list"]')


def add(frame: FrameLocator, text: str) -> None:
    frame.get_by_label("New to-do").fill(text)
    frame.get_by_role("button", name="Add").click()


def show_todos(page: Page) -> Any:
    """The list as the server holds it, asked through the API the panel uses."""
    return page.evaluate(
        """async ([server, tool, agent]) => {
            const answer = await fetch(`/api/apps/${server}/tools/${tool}`, {
                method: "POST",
                headers: {"content-type": "application/json"},
                body: JSON.stringify({agent, arguments: {}}),
            });
            return (await answer.json()).structuredContent;
        }""",
        [SERVER, TOOL, AGENT],
    )


def test_side_panel_and_an_app(server: str, page: Page) -> None:
    # 1. "New chat", pick the agent with the to-do list, expand the side panel: "Add a tab"
    #    offers the agent's app.
    page.goto(server)
    page.get_by_role("button", name="New chat").click()
    page.get_by_label("Agent").select_option(AGENT)
    page.get_by_role("button", name="Expand the side panel").click()
    assert menu(page) == ["To-do list"]
    page.keyboard.press("Escape")

    # 2. Open the app: its view is in a sandboxed frame, and shows the server's (empty) list.
    pick(page, "To-do list")
    expect(page.get_by_role("tab", name="To-do list")).to_have_attribute("aria-selected", "true")
    frame_element = page.locator('iframe[title="To-do list"]')
    expect(frame_element).to_have_attribute("sandbox", "allow-scripts")
    frame = view(page)
    expect(frame.get_by_text("Nothing to do.")).to_be_visible()

    # 3. Add two to-dos and tick the first, in the view: each goes to the server and back.
    add(frame, "milk")
    expect(frame.get_by_text("1 to-do, 0 done")).to_be_visible()
    add(frame, "eggs")
    expect(frame.get_by_text("2 to-dos, 0 done")).to_be_visible()
    frame.get_by_role("checkbox", name="milk").check()
    expect(frame.get_by_text("2 to-dos, 1 done")).to_be_visible()
    assert show_todos(page) == {
        "items": [
            {"id": "1", "text": "milk", "done": True},
            {"id": "2", "text": "eggs", "done": False},
        ]
    }
    # While a tick is on its way, the other to-dos stay as they are drawn. Disabled, a whole
    # list of boxes greys out and comes back.
    held: list[Route] = []
    page.route("**/tools/toggle_todo", lambda route: held.append(route))
    with page.expect_request("**/tools/toggle_todo"):
        frame.get_by_role("checkbox", name="eggs").click()
    expect(frame.get_by_role("checkbox", name="milk")).to_be_enabled()
    held[0].continue_()
    page.unroute("**/tools/toggle_todo")
    expect(frame.get_by_text("2 to-dos, 2 done")).to_be_visible()
    frame.get_by_role("checkbox", name="eggs").uncheck()
    expect(frame.get_by_text("2 to-dos, 1 done")).to_be_visible()

    # 4. The view follows the theme on the screen.
    page.get_by_role("button", name="Dark").click()
    expect(frame.locator("html")).to_have_attribute("data-theme", "dark")
    page.get_by_role("button", name="Light").click()
    expect(frame.locator("html")).to_have_attribute("data-theme", "light")

    # 5. Close the tab: it is offered again. Open it again: the list is the server's.
    page.get_by_role("button", name="Close To-do list").click()
    expect(frame_element).to_have_count(0)
    assert menu(page) == ["To-do list"]
    page.get_by_role("menuitem", name="To-do list").click()
    expect(view(page).get_by_text("2 to-dos, 1 done")).to_be_visible()
    expect(view(page).get_by_role("checkbox", name="milk")).to_be_checked()

    # 6. With the app open the menu has nothing left to offer.
    expect(page.get_by_role("tab")).to_have_text(["To-do list"])
    expect(page.get_by_role("button", name="Add a tab")).to_be_disabled()
    # Folded to the rail and opened again, the view is the one it was: it kept even what was
    # typed in it and not sent.
    view(page).get_by_label("New to-do").fill("bread")
    page.get_by_role("button", name="Collapse the side panel").click()
    expect(frame_element).to_be_hidden()
    page.get_by_role("button", name="Expand the side panel").click()
    expect(view(page).get_by_label("New to-do")).to_have_value("bread")
    expect(view(page).get_by_text("2 to-dos, 1 done")).to_be_visible()

    # 7. The agent adds to the list: the open view shows it when the turn ends, and keeps what
    #    is typed in it. The first message moves the tab to the new conversation, so the second
    #    is the one that reaches a view already on the screen.
    agent_answers = page.locator('[data-role="assistant"]')
    send(page, "butter")
    expect(agent_answers.first).to_contain_text("butter")
    expect(view(page).get_by_label("New to-do")).to_be_visible()
    view(page).get_by_label("New to-do").fill("cake")
    send(page, "jam")
    expect(agent_answers.nth(1)).to_contain_text("jam")
    expect(view(page).get_by_text("4 to-dos, 1 done")).to_be_visible()
    expect(view(page).get_by_role("checkbox", name="jam")).not_to_be_checked()
    expect(view(page).get_by_label("New to-do")).to_have_value("cake")
