# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What is written and not sent stays with the conversation it was written in: another
conversation opens on its own box, and the draft is there again on coming back, "New chat" in
between or not."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from util.browser import expect_thread, open_conversation, send
from util.fake_openai import FakeLocalGPTServer

pytestmark = [pytest.mark.io, pytest.mark.database]


def test_a_draft_stays_with_its_conversation(
    server: str, page: Page, local_gpt: FakeLocalGPTServer
) -> None:
    box = page.get_by_label("Message input")

    # 1. Two conversations.
    page.goto(server)
    for question in ("alpha", "bravo"):
        page.get_by_role("button", name="New chat").click()
        page.get_by_label("Agent").select_option("pydantic_ai")
        send(page, question)
        expect_thread(page, [(question, question)])

    # 2. A draft in "alpha"; "bravo" opens on an empty box, and takes a draft of its own.
    open_conversation(page, "alpha")
    expect_thread(page, [("alpha", "alpha")])
    box.fill("not sent in alpha")
    open_conversation(page, "bravo")
    expect_thread(page, [("bravo", "bravo")])
    expect(box).to_have_value("")
    box.fill("not sent in bravo")

    # 3. Each is there again on coming back, "New chat" in between too, which has a box of its
    #    own.
    open_conversation(page, "alpha")
    expect_thread(page, [("alpha", "alpha")])
    expect(box).to_have_value("not sent in alpha")
    page.get_by_role("button", name="New chat").click()
    expect(box).to_have_value("")
    open_conversation(page, "bravo")
    expect_thread(page, [("bravo", "bravo")])
    expect(box).to_have_value("not sent in bravo")

    # 4. A draft that is sent is no longer one.
    page.get_by_label("Send message").click()
    expect_thread(page, [("bravo", "bravo"), ("not sent in bravo", "not sent in bravo")])
    open_conversation(page, "alpha")
    expect(box).to_have_value("not sent in alpha")
    open_conversation(page, "bravo")
    expect_thread(page, [("bravo", "bravo"), ("not sent in bravo", "not sent in bravo")])
    expect(box).to_have_value("")
