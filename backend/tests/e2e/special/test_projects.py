# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Projects in the panel: a conversation goes in "Default" unless another project is chosen, a
project is made from the panel, and each project folds away under its heading."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from util.browser import expect_thread, send
from util.fake_openai import FakeLocalGPTServer

pytestmark = [pytest.mark.io, pytest.mark.database]


def test_projects_in_the_panel(server: str, page: Page, local_gpt: FakeLocalGPTServer) -> None:
    history = page.get_by_role("navigation", name="History")

    # 1. A first conversation, with no project chosen, is in "Default".
    page.goto(server)
    page.get_by_role("button", name="New chat").click()
    page.get_by_label("Agent").select_option("pydantic_ai")
    expect(page.get_by_label("Project").locator("option:checked")).to_have_text("Default")
    send(page, "in default")
    expect_thread(page, [("in default", "in default")])
    default = history.get_by_role("list", name="Default")
    expect(default.get_by_role("link", name="in default")).to_be_visible()

    # 2. "New project" makes "Work"; its "+" starts a chat in it.
    history.get_by_role("button", name="New project").click()
    history.get_by_role("textbox", name="Project name").fill("Work")
    history.get_by_role("button", name="Create the project").click()
    work = history.get_by_role("list", name="Work")
    expect(work).to_contain_text("No conversations yet.")
    history.get_by_role("button", name="Start a conversation in Work").click()
    send(page, "at work")
    expect_thread(page, [("at work", "at work")])
    expect(work.get_by_role("link", name="at work")).to_be_visible()
    expect(default.get_by_role("link", name="at work")).to_have_count(0)

    # 3. "Work" folds away, and is still folded after a reload; "Default" is not.
    work_heading = history.get_by_role("button", name=re.compile("^Work"))
    expect(work_heading).to_have_attribute("aria-expanded", "true")
    work_heading.click()
    expect(history.get_by_role("link", name="at work")).to_be_hidden()
    page.reload()
    expect(history.get_by_role("link", name="in default")).to_be_visible()
    expect(history.get_by_role("link", name="at work")).to_be_hidden()
    expect(work_heading).to_have_attribute("aria-expanded", "false")
    work_heading.click()
    expect(history.get_by_role("link", name="at work")).to_be_visible()
