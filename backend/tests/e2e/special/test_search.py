# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The search page: it opens on every conversation that is not archived, the box and the
filters narrow it, and the rows ticked -- or every match, on any page -- are archived or
deleted at once."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from util.browser import expect_thread, history, send, sign_in
from util.fake_openai import FakeLocalGPTServer
from util.stack import Server

pytestmark = [pytest.mark.io, pytest.mark.database]


def test_search_filters_and_bulk_actions(
    server: Server, page: Page, local_gpt: FakeLocalGPTServer
) -> None:
    panel = page.get_by_role("navigation", name="History")
    results = page.get_by_role("region", name="Search").get_by_role("table")

    # 1. Three conversations: two in "Default", one in a project "Work".
    sign_in(page, server)
    for text in ("budget one", "notes"):
        page.get_by_role("button", name="New chat").click()
        page.get_by_label("Agent").select_option("pydantic_ai")
        send(page, text)
        expect_thread(page, [(text, text)])
    panel.get_by_role("button", name="New project").click()
    panel.get_by_role("textbox", name="Project name").fill("Work")
    panel.get_by_role("button", name="Create the project").click()
    panel.get_by_role("button", name="Start a conversation in Work").click()
    page.get_by_label("Agent").select_option("pydantic_ai")
    send(page, "budget two")
    expect_thread(page, [("budget two", "budget two")])

    # 2. "Search" opens on all three, newest first, before anything is typed.
    page.get_by_role("button", name="Search").click()
    expect(results.get_by_role("link")).to_have_text(["budget two", "notes", "budget one"])

    # 3. Typing narrows it; the project filter narrows it further.
    box = page.get_by_role("searchbox", name="Search conversations")
    box.fill("budget")
    expect(results.get_by_role("link")).to_have_text(["budget two", "budget one"])
    page.get_by_role("combobox", name="Project").select_option(label="Work")
    expect(results.get_by_role("link")).to_have_text(["budget two"])
    page.get_by_role("combobox", name="Project").select_option(label="All projects")
    box.fill("")
    expect(results.get_by_role("link")).to_have_text(["budget two", "notes", "budget one"])

    # 4. A ticked row is archived: off the page and off the panel.
    page.get_by_role("checkbox", name="Select notes").check()
    page.get_by_role("button", name="Archive").click()
    expect(page.get_by_text("Archived 1 conversation.")).to_be_visible()
    expect(results.get_by_role("link")).to_have_text(["budget two", "budget one"])
    expect(history(page)).to_have_count(2)
    expect(history(page).filter(has_text="notes")).to_have_count(0)

    # 5. "Include archived" brings it back, marked.
    page.get_by_role("combobox", name="Include archived").select_option("Yes")
    expect(results.get_by_role("link")).to_have_text(["budget two", "notes", "budget one"])
    expect(results.get_by_role("row").filter(has_text="notes")).to_contain_text("Archived")

    # 6. "Select all", one row unticked, and "Delete" -- which asks first -- deletes the rest.
    page.get_by_role("checkbox", name="Select all matching conversations").check()
    page.get_by_role("checkbox", name="Select notes").uncheck()
    page.get_by_role("button", name="Delete").click()
    page.get_by_role("button", name="Yes, delete").click()
    expect(page.get_by_text("Deleted 2 conversations.")).to_be_visible()
    expect(results.get_by_role("link")).to_have_text(["notes"])
    expect(history(page)).to_have_count(0)
