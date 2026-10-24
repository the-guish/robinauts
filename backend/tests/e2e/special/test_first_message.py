# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Editing and regenerating the first message of a conversation: the turn that has no answer
above it to continue from, so the model must be sent nothing of what came before."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from util import browser
from util.browser import answers, expect_thread, send
from util.fake_openai import FakeLocalGPTServer
from util.stack import sent

pytestmark = [pytest.mark.io, pytest.mark.database]


def aux_first_message(agent: str, server: str, page: Page, local_gpt: FakeLocalGPTServer) -> None:
    calls = local_gpt.received

    # 1. "New chat", pick the agent, send two messages.
    page.goto(server)
    page.get_by_role("button", name="New chat").click()
    page.get_by_label("Agent").select_option(agent)
    send(page, "one")
    expect_thread(page, [("one", "one")])
    send(page, "two")
    expect_thread(page, [("one", "one"), ("two", "two")])

    # 2. Edit the first message (hover it, "Edit", "Update"): the whole thread is replaced, and
    #    the model is sent the new question alone.
    browser.edit(page, "one", "ONE")
    expect_thread(page, [("ONE", "ONE")])
    assert sent(calls[-1]) == [("user", "ONE")]

    # 3. Regenerate its answer ("Refresh"): a new answer, and still the question alone is sent.
    regenerated = answers(page).last.locator("xpath=ancestor::*[@data-role][1]")
    first_id = regenerated.get_attribute("data-message-id")
    browser.regenerate_last(page)
    expect(regenerated).not_to_have_attribute("data-message-id", first_id or "")
    expect_thread(page, [("ONE", "ONE")])
    assert sent(calls[-1]) == [("user", "ONE")]

    # 4. Reload: the edited thread is what was stored.
    page.reload()
    expect_thread(page, [("ONE", "ONE")])
    assert len(calls) == 4


def test_pydantic_ai_first_message(server: str, page: Page, local_gpt: FakeLocalGPTServer) -> None:
    aux_first_message("pydantic_ai", server, page, local_gpt)


def test_langchain_first_message(server: str, page: Page, local_gpt: FakeLocalGPTServer) -> None:
    aux_first_message("langchain", server, page, local_gpt)


def test_kataloop_first_message(server: str, page: Page, local_gpt: FakeLocalGPTServer) -> None:
    aux_first_message("kataloop", server, page, local_gpt)
