# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The everyday operations in a real browser, through each engine to an OpenAI-compatible server,
kept in PostgreSQL.

The server is ``FakeLocalGPTServer`` on this machine, answering with ``EchoModel``: every answer is
the question it answers, and what the engine sent it shows what the engine remembered.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from util import browser
from util.browser import answers, expect_thread, history, send
from util.fake_openai import FakeLocalGPTServer
from util.stack import sent

pytestmark = [pytest.mark.io, pytest.mark.database]


def echoed(*questions: str) -> list[tuple[str, str]]:
    return [(q, q) for q in questions]


def history_of(*questions: str) -> list[tuple[str, str]]:
    """What the model is sent for these questions, each but the last with its echoed answer."""
    *answered, last = questions
    return [turn for q in answered for turn in (("user", q), ("assistant", q))] + [("user", last)]


def aux_master_journey(agent: str, server: str, page: Page, local_gpt: FakeLocalGPTServer) -> None:
    """Two conversations walked the way a person would, on the agent with that id.

    Every step checks the screen, and every turn also checks the request the model server got
    (``last_call``): which model, and the exact history the engine remembered.
    """
    calls = local_gpt.received

    def last_call(model: str, *questions: str) -> None:
        assert calls[-1]["model"] == model
        assert calls[-1]["stream"] is True
        assert sent(calls[-1]) == history_of(*questions)

    # --- Conversation A ---------------------------------------------------------------------

    # 1. "New chat" in the panel, then pick the agent in the "Agent" select.
    page.goto(server)
    page.get_by_role("button", name="New chat").click()
    page.get_by_label("Agent").select_option(agent)

    # 2. Send a first message ("Message input", "Send message"): it is answered, and the
    #    conversation appears in the panel's "Conversations" list, titled after it.
    send(page, "alpha one")
    expect_thread(page, echoed("alpha one"))
    expect(history(page)).to_have_text(["alpha one"])
    last_call("fake-gpt", "alpha one")

    # 3. Send two more: the model is sent the whole conversation so far.
    send(page, "alpha two")
    expect_thread(page, echoed("alpha one", "alpha two"))
    send(page, "alpha three")
    expect_thread(page, echoed("alpha one", "alpha two", "alpha three"))
    last_call("fake-gpt", "alpha one", "alpha two", "alpha three")

    # 4. Edit the middle message (hover it, "Edit", type, "Update"): everything after it is
    #    cut, on the screen and in what the model is sent, and the edit is answered.
    browser.edit(page, "alpha two", "alpha TWO")
    expect_thread(page, echoed("alpha one", "alpha TWO"))
    last_call("fake-gpt", "alpha one", "alpha TWO")
    assert len(calls) == 4

    # 5. Regenerate the last answer (hover it, "Refresh"): a new answer replaces it, with the
    #    same history sent again and no question repeated.
    regenerated = answers(page).last.locator("xpath=ancestor::*[@data-role][1]")
    first_id = regenerated.get_attribute("data-message-id")
    browser.regenerate_last(page)
    expect(regenerated).not_to_have_attribute("data-message-id", first_id or "")
    expect_thread(page, echoed("alpha one", "alpha TWO"))
    assert len(calls) == 5
    last_call("fake-gpt", "alpha one", "alpha TWO")
    a_url = page.url

    # --- Conversation B ---------------------------------------------------------------------

    # 6. "New chat" again: an empty thread; the first message makes a second conversation,
    #    listed above A in "Conversations", and nothing of A is sent to the model.
    page.get_by_role("button", name="New chat").click()
    expect_thread(page, [])
    page.get_by_label("Agent").select_option(agent)
    send(page, "beta one")
    expect_thread(page, echoed("beta one"))
    expect(history(page)).to_have_text(["beta one", "alpha one"])
    last_call("fake-gpt", "beta one")
    b_url = page.url

    # 7. Change the open conversation's model in the "Model" select under its title: the
    #    next message goes to the other model, with B's history.
    page.get_by_label("Model").select_option("local_gpt_2")
    expect(page.get_by_label("Model")).not_to_have_attribute("aria-busy", "true")
    send(page, "beta two")
    expect_thread(page, echoed("beta one", "beta two"))
    last_call("fake-gpt-2", "beta one", "beta two")

    # --- Back to A --------------------------------------------------------------------------

    # 8. Open A from "Conversations": its own address, its edited thread, its own model.
    browser.open_conversation(page, "alpha one")
    expect(page).to_have_url(a_url)
    expect_thread(page, echoed("alpha one", "alpha TWO"))
    expect(page.get_by_label("Model")).to_have_value("local_gpt")

    # 9. Send a message in A: it goes on from the edited history, to A's model.
    send(page, "alpha four")
    expect_thread(page, echoed("alpha one", "alpha TWO", "alpha four"))
    last_call("fake-gpt", "alpha one", "alpha TWO", "alpha four")

    # --- Managing conversations -------------------------------------------------------------

    # 10. Delete B from its row ("Actions for beta one", "Delete"): first "No, keep it", which
    #     keeps it, then "Yes, delete", which takes it out of "Conversations".
    browser.conversation_action(page, "beta one", "Delete")
    page.get_by_role("button", name="No, keep it: beta one").click()
    expect(history(page)).to_have_text(["alpha one", "beta one"])
    browser.conversation_action(page, "beta one", "Delete")
    page.get_by_role("button", name="Yes, delete: beta one").click()
    expect(history(page)).to_have_text(["alpha one"])

    # 11. Rename A from its row ("Actions for alpha one", "Rename", the "Title" box): first
    #     "Cancel the rename", which keeps the old title, then "Save the title", which changes
    #     both the row and the page heading.
    browser.conversation_action(page, "alpha one", "Rename")
    page.get_by_role("textbox", name="Title", exact=True).fill("Abandoned")
    page.get_by_role("button", name="Cancel the rename").click()
    expect(history(page)).to_have_text(["alpha one"])
    browser.conversation_action(page, "alpha one", "Rename")
    page.get_by_role("textbox", name="Title", exact=True).fill("Project alpha")
    page.get_by_role("button", name="Save the title").click()
    expect(history(page)).to_have_text(["Project alpha"])
    expect(page.get_by_role("heading", level=1)).to_have_text("Project alpha")

    # --- Persistence ------------------------------------------------------------------------

    # 12. Reload the page: title, list, thread and model come back from the database as left.
    page.reload()
    expect(page.get_by_role("heading", level=1)).to_have_text("Project alpha")
    expect(history(page)).to_have_text(["Project alpha"])
    expect_thread(page, echoed("alpha one", "alpha TWO", "alpha four"))
    expect(page.get_by_label("Model")).to_have_value("local_gpt")

    # 13. Go to deleted B's address: the page says it is not here. No model call was made
    #     beyond the eight turns above.
    page.goto(b_url)
    expect(page.get_by_role("alert")).to_contain_text("This conversation is not here.")
    assert len(calls) == 8


def test_langchain_master_journey(server: str, page: Page, local_gpt: FakeLocalGPTServer) -> None:
    aux_master_journey("langchain", server, page, local_gpt)


def test_pydantic_ai_master_journey(server: str, page: Page, local_gpt: FakeLocalGPTServer) -> None:
    aux_master_journey("pydantic_ai", server, page, local_gpt)


def test_kataloop_master_journey(server: str, page: Page, local_gpt: FakeLocalGPTServer) -> None:
    aux_master_journey("kataloop", server, page, local_gpt)
