# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A stopped answer, through each engine and a real MCP server: it keeps what it had done, says
in its place that it was stopped, and the message after it tells the model what that was.

``HoldsItsAnswer`` calls the MCP server's ``shout`` with the question "look", then holds the
call that brings the tool's result, which is when the test stops the turn: a turn stopped with
a tool round made. Anything else it echoes.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Any

import pytest
from playwright.sync_api import Page, expect

from util.browser import STOPPED, send, sign_in
from util.fake_openai import CallTool, FakeLocalGPTServer
from util.fake_openai.echo_model import last_question
from util.mcp_server import mcp_server
from util.stack import Server, sent

pytestmark = [pytest.mark.io, pytest.mark.database]

# What the next message is told of the stopped answer (worker/failures.py).
TOLD = "The user stopped your answer to it before it finished."
SHOUT_CALL = '- shout({"text": "look"}) -> result, tool output: "LOOK"'


class HoldsItsAnswer:
    def __init__(self) -> None:
        self.holding = threading.Event()
        self.release = threading.Event()

    def reply(self, messages: list[dict[str, Any]]) -> str | CallTool:
        question = last_question(messages)
        if question != "look":
            return question
        if messages[-1]["role"] != "tool":
            return CallTool("shout", {"text": question})
        self.holding.set()
        self.release.wait(30)
        return "too late"


@pytest.fixture
def fake_model() -> HoldsItsAnswer:
    return HoldsItsAnswer()


@pytest.fixture
def tools_url() -> Iterator[str]:
    def shout(text: str) -> str:
        """The text, in capitals."""
        return text.upper()

    with mcp_server(shout) as url:
        yield url


def expect_shout_used(page: Page) -> None:
    """The stopped answer's one call, under "1 tool call": made, with its result."""
    answer = page.locator('[data-role="assistant"]').first
    answer.get_by_role("button", name="1 tool call").click()
    expect(answer.locator('[data-slot="tool-fallback-trigger"]')).to_have_text(["Used tool: shout"])


def aux_stopped_answer(
    agent: str, server: Server, page: Page, local_gpt: FakeLocalGPTServer, model: HoldsItsAnswer
) -> None:
    calls = local_gpt.received
    answers = page.locator('[data-role="assistant"]')
    stopped = answers.first.get_by_text(STOPPED)

    # 1. "New chat", pick the agent with tools, send "look": the model calls "shout", the tool
    #    runs, and the model holds the call that brings its result. "Stop generating": the
    #    answer keeps the call it made, and says in its place, once, that it was stopped.
    sign_in(page, server)
    page.get_by_role("button", name="New chat").click()
    page.get_by_label("Agent").select_option(f"{agent}_tools")
    send(page, "look")
    try:
        assert model.holding.wait(15)
        page.get_by_label("Stop generating").click()
        expect(stopped).to_be_visible()
    finally:
        model.release.set()
    expect(page.get_by_text(STOPPED)).to_have_count(1)
    expect_shout_used(page)

    # 2. Reload: the question, the stopped answer and its call come back from the database.
    page.reload()
    expect(page.locator('[data-role="user"]')).to_contain_text("look")
    expect(stopped).to_be_visible()
    expect(page.get_by_text(STOPPED)).to_have_count(1)
    expect_shout_used(page)

    # 3. Send "not that way": it goes under the stopped answer, which stays and still says it
    #    was stopped. The engine remembers nothing of the stopped turn, so the model gets one
    #    message: the stopped exchange with the call it made, then the new message. It echoes it.
    send(page, "not that way")
    expect(answers).to_have_count(2)
    expect(answers.last).to_contain_text("Message: look")
    expect(answers.last).to_contain_text("not that way")
    expect(stopped).to_be_visible()
    reply = sent(calls[-1])
    assert [role for role, _ in reply] == ["user"], reply
    assert "Message: look" in reply[0][1], reply
    assert TOLD in reply[0][1], reply
    assert SHOUT_CALL in reply[0][1], reply
    assert reply[0][1].endswith("not that way"), reply

    # 4. Reload: the reply and the stopped answer above it are both still there.
    page.reload()
    expect(answers).to_have_count(2)
    expect(page.locator('[data-role="user"]')).to_have_count(2)
    expect(answers.last).to_contain_text("not that way")
    expect(stopped).to_be_visible()
    expect(page.get_by_text(STOPPED)).to_have_count(1)


def test_pydantic_ai_stopped_answer(
    server: Server, page: Page, local_gpt: FakeLocalGPTServer, fake_model: HoldsItsAnswer
) -> None:
    aux_stopped_answer("pydantic_ai", server, page, local_gpt, fake_model)


def test_langchain_stopped_answer(
    server: Server, page: Page, local_gpt: FakeLocalGPTServer, fake_model: HoldsItsAnswer
) -> None:
    aux_stopped_answer("langchain", server, page, local_gpt, fake_model)
