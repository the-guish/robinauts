# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The right side panel, and MCP apps in it (``docs/architecture/mcp-apps.md``): the to-do list
of ``mcp/to-do-list/`` and the diagrams of ``mcp/diagrams/``, each run as its own command
beside the platform, as an operator would run it.

The [+] menu offers the agent's apps. A call of the agent is a sub-tab: its view opens in a
sandboxed frame, is told the call's arguments and its result, talks to the panel, and through
it to the server.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import FrameLocator, Page, Request, Route, expect

from util.browser import rail, send, sign_in
from util.fake_openai import CallTool, FakeModel
from util.fake_openai.echo_model import last_question, text_of
from util.mcp_server import example_server
from util.stack import Server

pytestmark = [pytest.mark.io, pytest.mark.database]

AGENT = "pydantic_ai_tools"
# The id the configuration of ``util.stack`` gives the MCP server, and the to-do list's tool.
SERVER = "tools"
TOOL = "show_todos"

DIAGRAM_ID = re.compile(r"\(diagram_id ([0-9a-f]+)\)")


def asked_and_answered(messages: list[dict[str, Any]]) -> tuple[str, int]:
    """The last question, and how many tool results the model has been sent since it."""
    since = 0
    for message in reversed(messages):
        if message["role"] == "user":
            break
        since += message["role"] == "tool"
    return last_question(messages), since


class KeepsTheList:
    """A model that shows the list when asked to "show", and adds any other message to it."""

    def reply(self, messages: list[dict[str, Any]]) -> str | CallTool:
        question, answered = asked_and_answered(messages)
        if answered:
            return f"Done: {question}"
        if question.startswith("show"):
            return CallTool("show_todos", {}, f"call_{len(messages)}")
        return CallTool("add_todo", {"text": question}, f"call_{len(messages)}")


class Draws:
    """A model that draws and shows a diagram of the nodes a "draw" names, one arrow from each
    to the next; adds a node to the first diagram on "add"; and shows the first on "show"."""

    def reply(self, messages: list[dict[str, Any]]) -> str | CallTool:
        question, answered = asked_and_answered(messages)
        said = " ".join(text_of(m) for m in messages if m["role"] == "tool")
        diagrams = list(dict.fromkeys(DIAGRAM_ID.findall(said)))
        call_id = f"call_{len(messages)}"
        verb, _, rest = question.partition(" ")
        steps: list[CallTool] = []
        if verb == "draw":
            names = rest.split()
            nodes = [{"id": name} for name in names]
            arrows = [{"source": a, "target": b} for a, b in zip(names, names[1:], strict=False)]
            steps = [CallTool("draw_diagram", {"nodes": nodes, "arrows": arrows}, call_id)]
            if answered:
                steps.append(CallTool("show_diagram", {"diagram_id": diagrams[-1]}, call_id))
        elif verb == "add":
            added = {"diagram_id": diagrams[0], "nodes": [{"id": rest}]}
            steps = [CallTool("add_to_diagram", added, call_id)]
        elif verb == "show":
            steps = [CallTool("show_diagram", {"diagram_id": diagrams[0]}, call_id)]
        return steps[answered] if answered < len(steps) else f"Done: {question}"


@pytest.fixture
def fake_model(request: pytest.FixtureRequest) -> FakeModel:
    """The model of the test: ``KeepsTheList`` unless the test asks for another."""
    model: FakeModel = getattr(request, "param", KeepsTheList)()
    return model


@pytest.fixture
def tools_url(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[str]:
    """The MCP server of the test: the to-do list unless the test asks for the diagrams."""
    if getattr(request, "param", "to-do-list") == "diagrams":
        with example_server("diagrams", "robinauts-diagrams") as url:
            yield url
        return
    todos = str(tmp_path / "todos.json")
    with example_server("to-do-list", "robinauts-to-do-list", "--file", todos) as url:
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


def view(page: Page, title: str = "To-do list 1") -> FrameLocator:
    return page.frame_locator(f'iframe[title="{title}"]')


def add(frame: FrameLocator, text: str) -> None:
    frame.get_by_label("New to-do").fill(text)
    frame.get_by_role("button", name="Add").click()


def show_todos(page: Page) -> Any:
    """The list as the server holds it, asked through the API the panel uses."""
    return page.evaluate(
        """async ([server, tool, agent]) => {
            const answer = await fetch(`/robinauts/api/apps/${server}/tools/${tool}`, {
                method: "POST",
                headers: {"content-type": "application/json"},
                body: JSON.stringify({agent, arguments: {}}),
            });
            return (await answer.json()).structuredContent;
        }""",
        [SERVER, TOOL, AGENT],
    )


def test_side_panel_and_an_app(server: Server, page: Page) -> None:
    # 1. "New chat", pick the agent with the to-do list, expand the side panel: "Add a tab"
    #    offers the agent's app. Added by hand, its tab is empty: the agent has shown nothing.
    sign_in(page, server)
    page.get_by_role("button", name="New chat").click()
    page.get_by_label("Agent").select_option(AGENT)
    page.get_by_role("button", name="Expand the side panel").click()
    assert menu(page) == ["To-do list"]
    page.keyboard.press("Escape")
    pick(page, "To-do list")
    expect(page.get_by_role("tab", name="To-do list", exact=True)).to_have_attribute(
        "aria-selected", "true"
    )
    expect(page.get_by_text("To-do list shows here when the agent uses it.")).to_be_visible()

    # 2. The agent shows the list: its call is a sub-tab of that tab, with the view in a
    #    sandboxed frame, told the server's (empty) list.
    agent_answers = page.locator('[data-role="assistant"]')
    send(page, "show the list")
    expect(agent_answers.first).to_contain_text("Done: show the list")
    expect(page.get_by_role("tab", name="To-do list 1")).to_have_attribute("aria-selected", "true")
    frame_element = page.locator('iframe[title="To-do list 1"]')
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

    # 4. The view follows the theme on the screen, which the shell's toggle on the rail moves
    #    from the system's to light, then to dark.
    theme = rail(page).get_by_role("button", name=re.compile("^Theme: "))
    theme.click()
    expect(frame.locator("html")).to_have_attribute("data-theme", "light")
    theme.click()
    expect(frame.locator("html")).to_have_attribute("data-theme", "dark")

    # 5. Close the tab: it is offered again. Open it again: the conversation's sub-tab is back,
    #    and its view shows the server's list.
    page.get_by_role("button", name="Close To-do list", exact=True).click()
    expect(frame_element).to_have_count(0)
    assert menu(page) == ["To-do list"]
    page.get_by_role("menuitem", name="To-do list").click()
    expect(view(page).get_by_text("2 to-dos, 1 done")).to_be_visible()
    expect(view(page).get_by_role("checkbox", name="milk")).to_be_checked()

    # 6. With the app open the menu has nothing left to offer.
    expect(page.get_by_role("button", name="Add a tab")).to_be_disabled()
    # Folded to the rail and opened again, the view is the one it was: it kept even what was
    # typed in it and not sent.
    view(page).get_by_label("New to-do").fill("bread")
    page.get_by_role("button", name="Collapse the side panel").click()
    expect(frame_element).to_be_hidden()
    page.get_by_role("button", name="Expand the side panel").click()
    expect(view(page).get_by_label("New to-do")).to_have_value("bread")
    expect(view(page).get_by_text("2 to-dos, 1 done")).to_be_visible()

    # 7. The agent adds to the list, with a tool that has no view: the open view reads the
    #    list again by itself, and keeps what is typed in it.
    send(page, "butter")
    expect(agent_answers.nth(1)).to_contain_text("Done: butter")
    expect(view(page).get_by_text("3 to-dos, 1 done")).to_be_visible()
    expect(view(page).get_by_label("New to-do")).to_have_value("bread")

    # 8. The agent shows the list again, with the panel collapsed: the same call, so the same
    #    sub-tab and the same view, and the panel stays collapsed.
    page.get_by_role("button", name="Collapse the side panel").click()
    send(page, "show it again")
    expect(agent_answers.nth(2)).to_contain_text("Done: show it again")
    expect(page.get_by_role("button", name="Expand the side panel")).to_be_visible()
    page.get_by_role("button", name="Expand the side panel").click()
    expect(page.get_by_role("tablist", name="To-do list, shown").get_by_role("tab")).to_have_text(
        ["To-do list 1"]
    )
    expect(view(page).get_by_label("New to-do")).to_have_value("bread")

    # 9. The page loaded again: the tab is remembered, and its sub-tab comes from the
    #    conversation.
    page.reload()
    expect(page.get_by_role("tab", name="To-do list 1")).to_have_attribute("aria-selected", "true")
    expect(view(page).get_by_text("3 to-dos, 1 done")).to_be_visible()


def sub_tabs(page: Page) -> list[str]:
    strip = page.get_by_role("tablist", name="Diagrams, shown")
    return strip.get_by_role("tab").all_text_contents()


def on_top(page: Page) -> str:
    strip = page.get_by_role("tablist", name="Diagrams, shown")
    return strip.locator('[role="tab"][aria-selected="true"]').inner_text()


def drawn(page: Page, title: str) -> Any:
    """What the view of that sub-tab says it shows."""
    return view(page, title).get_by_role("status")


@pytest.mark.parametrize(("fake_model", "tools_url"), [(Draws, "diagrams")], indirect=True)
def test_the_agent_s_diagrams_are_sub_tabs(server: Server, page: Page) -> None:
    # 1. "New chat", with the agent that has the diagrams, on the other engine. The panel is
    #    open and has no tab: nobody added one.
    sign_in(page, server)
    page.get_by_role("button", name="New chat").click()
    page.get_by_label("Agent").select_option("langchain_tools")
    page.get_by_role("button", name="Expand the side panel").click()
    assert menu(page) == ["Diagrams"]
    page.keyboard.press("Escape")
    agent_answers = page.locator('[data-role="assistant"]')

    # 2. The agent draws a diagram and shows it: the tab opens by itself, with one sub-tab,
    #    whose view is told the diagram the agent's own call was answered with.
    reads: list[Request] = []
    results: list[Request] = []
    page.on("request", lambda r: reads.append(r) if r.url.endswith("/tools/show_diagram") else 0)
    page.on("request", lambda r: results.append(r) if "/tool-result?call_id=" in r.url else 0)
    send(page, "draw a b")
    expect(agent_answers.first).to_contain_text("Done: draw a b")
    expect(page.get_by_role("tab", name="Diagrams", exact=True)).to_have_attribute(
        "aria-selected", "true"
    )
    assert sub_tabs(page) == ["Diagram 1"]
    expect(drawn(page, "Diagram 1")).to_be_visible()
    expect(drawn(page, "Diagram 1")).to_have_text("Diagram: 2 nodes, 1 arrows.")
    # One read of the agent's result, for the one call with a view: drawing has none.
    assert len(results) == 1, [r.url for r in results]
    expect(page.locator('iframe[title="Diagram 1"]')).to_have_attribute("sandbox", "allow-scripts")

    # 3. A second diagram is a second sub-tab, and the one on top.
    send(page, "draw x y z")
    expect(agent_answers.nth(1)).to_contain_text("Done: draw x y z")
    expect(drawn(page, "Diagram 2")).to_have_text("Diagram: 3 nodes, 2 arrows.")
    assert sub_tabs(page) == ["Diagram 1", "Diagram 2"]
    assert on_top(page) == "Diagram 2"

    # 4. The agent adds to the first diagram, with a tool that has no view. The first view is
    #    not drawn, so it reads nothing: only the view on top asks the server.
    send(page, "add c")
    expect(agent_answers.nth(2)).to_contain_text("Done: add c")
    reads.clear()
    # Two rounds of the view on top, however long the runner takes over them: a hidden view
    # that read would have asked in that time too.
    for _ in range(2):
        with page.expect_request(lambda r: r.url.endswith("/tools/show_diagram"), timeout=30_000):
            pass
    asked = {r.post_data_json["arguments"]["diagram_id"] for r in reads}
    assert len(asked) == 1, asked
    # On top again, the first view reads its diagram and shows what the agent added.
    page.get_by_role("tab", name="Diagram 1").click()
    expect(drawn(page, "Diagram 1")).to_have_text("Diagram: 3 nodes, 1 arrows.")

    # 5. The agent shows the first diagram again while the second is on top: the same call,
    #    so its sub-tab comes back on top, and there are still two.
    page.get_by_role("tab", name="Diagram 2").click()
    assert on_top(page) == "Diagram 2"
    send(page, "show the first")
    expect(agent_answers.nth(3)).to_contain_text("Done: show the first")
    expect(page.get_by_role("tab", name="Diagram 1")).to_have_attribute("aria-selected", "true")
    assert sub_tabs(page) == ["Diagram 1", "Diagram 2"]

    # 6. The second sub-tab is closed: the first stays, under its name.
    page.get_by_role("button", name="Close Diagram 2").click()
    assert sub_tabs(page) == ["Diagram 1"]
    expect(page.locator('iframe[title="Diagram 2"]')).to_have_count(0)

    # 7. The conversation left for a new chat and opened again: its sub-tabs are rebuilt from
    #    its tool calls, the closed one stays closed, and the view shows its diagram.
    page.get_by_role("button", name="New chat").click()
    expect(page.get_by_role("tablist", name="Diagrams, shown")).to_have_count(0)
    page.get_by_role("link", name="draw a b").click()
    expect(page.get_by_role("tab", name="Diagram 1")).to_have_attribute("aria-selected", "true")
    assert sub_tabs(page) == ["Diagram 1"]
    expect(drawn(page, "Diagram 1")).to_have_text("Diagram: 3 nodes, 1 arrows.")

    # 8. The last sub-tab is closed with the keyboard: the tab is empty. The agent shows that
    #    diagram again, and its sub-tab is back.
    page.get_by_role("tab", name="Diagram 1").press("Delete")
    expect(page.get_by_text("Diagrams shows here when the agent uses it.")).to_be_visible()
    send(page, "show the first")
    expect(agent_answers.nth(4)).to_contain_text("Done: show the first")
    expect(page.get_by_role("tab", name="Diagram 1")).to_have_attribute("aria-selected", "true")
    assert sub_tabs(page) == ["Diagram 1"]
    expect(drawn(page, "Diagram 1")).to_have_text("Diagram: 3 nodes, 1 arrows.")
