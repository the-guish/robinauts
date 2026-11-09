# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A page left open follows what another client does through the API (``docs/specs/wire.md``,
"The change feed"). The page is never reloaded and never navigated in this journey.

Another client is an ``httpx`` client on the same server, which in the local development mode
is the same user. It needs a model that holds a turn open, and a client beside the browser,
which the master journey has neither of.
"""

from __future__ import annotations

import threading
import time

import httpx
import pytest
from playwright.sync_api import Page, expect

from util.browser import expect_thread, history, questions, send
from util.fake_openai import FakeModel, HoldingEchoModel

pytestmark = [pytest.mark.io, pytest.mark.database]

AGENT = "langchain"


@pytest.fixture
def fake_model() -> FakeModel:
    return HoldingEchoModel()


def echoed(*questions: str) -> list[tuple[str, str]]:
    return [(q, q) for q in questions]


def ask(api: httpx.Client, conversation: str, text: str) -> None:
    """A turn under the end of that conversation, started by the other client and read to its
    end."""
    last = api.get(f"/api/conversations/{conversation}").json()["messages"][-1]["id"]
    answered = api.post(
        f"/api/conversations/{conversation}/turns", json={"parent_id": last, "text": text}
    )
    assert answered.status_code == 200, answered.text


def test_a_page_left_open_follows_another_client(
    server: str, page: Page, fake_model: HoldingEchoModel
) -> None:
    api = httpx.Client(base_url=server, timeout=30)
    running = page.get_by_label("Conversations").get_by_role("img", name="Answering")

    # 1. The page starts a conversation of its own and keeps it open.
    page.goto(server)
    page.get_by_role("button", name="New chat").click()
    page.get_by_label("Agent").select_option(AGENT)
    send(page, "one")
    expect_thread(page, echoed("one"))
    conversation = page.url.rsplit("/", 1)[-1]

    # 2. Something is typed in the box and left unsent.
    page.get_by_label("Message input").fill("a draft")

    # 3. The other client sends a turn in the conversation on the screen: it appears, and the
    #    draft stays in the box.
    ask(api, conversation, "two")
    expect_thread(page, echoed("one", "two"))
    expect(page.get_by_label("Message input")).to_have_value("a draft")

    # 4. A few seconds after that turn ended, the other client sends one more: it appears too.
    time.sleep(3)
    ask(api, conversation, "three")
    expect_thread(page, echoed("one", "two", "three"))

    # 5. An edit box is open on the first question when the other client sends again: the
    #    answer appears, and the edit box keeps what was typed in it.
    message = page.locator('[data-role="user"]').filter(has_text="one")
    message.hover()
    message.get_by_role("button", name="Edit").click()
    page.get_by_role("textbox", name="Edit message").fill("one, half edited")
    ask(api, conversation, "four")
    expect(questions(page)).to_have_text(["two", "three", "four"])
    expect(page.get_by_role("textbox", name="Edit message")).to_have_value("one, half edited")
    page.get_by_role("button", name="Cancel").click()
    expect_thread(page, echoed("one", "two", "three", "four"))

    # 6. The other client starts a conversation: it appears in "Conversations", on top.
    started = api.post("/api/turns", json={"agent_id": AGENT, "text": "elsewhere"})
    assert started.status_code == 200, started.text
    elsewhere = started.headers["x-robinauts-conversation-id"]
    expect(history(page)).to_have_text(["elsewhere", "one"])

    # 7. A turn in that conversation, which is not on the screen, holds: its row shows it is
    #    answering, and stops showing it once the turn ends.
    def held_turn() -> None:
        ask(api, elsewhere, "hold")

    holding = threading.Thread(target=held_turn)
    holding.start()
    assert fake_model.asked.wait(10)
    expect(running).to_have_count(1)
    expect(history(page).first).to_have_text("elsewhere")
    fake_model.release.set()
    holding.join(10)
    expect(running).to_have_count(0)
    expect_thread(page, echoed("one", "two", "three", "four"))

    # 8. The page's own turn streams as before, drawn once.
    page.get_by_label("Message input").fill("")
    send(page, "five")
    expect_thread(page, echoed("one", "two", "three", "four", "five"))
    expect(page.locator('[data-role="user"]')).to_have_count(5)
    expect(page.locator('[data-role="assistant"]')).to_have_count(5)
    api.close()
