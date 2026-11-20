# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A model picked on an open conversation while its answer is still streaming is the model of the
next turn (``docs/specs/frontend.md``: "changing it applies from the next turn, a run in flight
included").

The run's end dates the conversation later than the pick and still has it on the old model. The
model server holds the answer to ``hold`` open after its text, so the pick lands inside the run.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from util.browser import expect_thread, send, sign_in
from util.fake_openai import FakeLocalGPTServer, FakeModel, StreamHoldingEchoModel
from util.stack import Server

pytestmark = [pytest.mark.io, pytest.mark.database]

AGENT = "pydantic_ai"


@pytest.fixture
def fake_model() -> FakeModel:
    return StreamHoldingEchoModel()


def test_a_model_picked_while_an_answer_streams_runs_the_next_turn(
    server: Server, page: Page, local_gpt: FakeLocalGPTServer, fake_model: StreamHoldingEchoModel
) -> None:
    calls = local_gpt.received
    stop = page.get_by_label("Stop generating")
    picker = page.get_by_label("Model")

    # 1. A conversation on the agent's default, "Local GPT".
    sign_in(page, server)
    page.get_by_role("button", name="New chat").click()
    page.get_by_label("Agent").select_option(AGENT)
    send(page, "one")
    expect_thread(page, [("one", "one")])

    # 2. "hold" is answered, and its run stays open: the answer is on the screen, and the stop
    #    button with it.
    send(page, "hold")
    assert fake_model.sent.wait(30)
    expect_thread(page, [("one", "one"), ("hold", "hold")])
    expect(stop).to_be_visible()

    # 3. "Local GPT 2" is picked while the run goes on, and saved.
    picker.select_option("local_gpt_2")
    expect(picker).not_to_have_attribute("aria-busy", "true")
    expect(stop).to_be_visible()

    # 4. The run ends, and the page reads the conversation again, dated after the pick and on
    #    "Local GPT": the pick stays.
    conversation = page.url.rsplit("/", 1)[-1]
    with page.expect_response(
        lambda r: r.request.method == "GET"
        and r.url.endswith(f"/robinauts/api/conversations/{conversation}")
    ) as read:
        fake_model.release.set()
    assert read.value.json()["conversation"]["model"] == "local_gpt"
    expect(stop).to_be_hidden()
    expect(picker).to_have_value("local_gpt_2")

    # 5. The next message goes to "Local GPT 2".
    send(page, "two")
    expect_thread(page, [("one", "one"), ("hold", "hold"), ("two", "two")])
    assert [call["model"] for call in calls] == ["fake-gpt", "fake-gpt", "fake-gpt-2"]
    expect(picker).to_have_value("local_gpt_2")
