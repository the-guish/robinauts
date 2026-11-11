# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Tool calls in the interface, through each engine and a real MCP server: a tool round that
answers, one whose turn fails after the tool ran, and two rounds in one answer.

``ToolEchoModel`` calls the MCP server's ``shout`` with the question, then answers with what it
said; a question starting with ``poison`` fails once the tool has run, and ``<a> then <b>`` makes
one round per piece. ``shout("hold")`` waits until the test releases it.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Any

import pytest
from playwright.sync_api import Locator, Page, expect

from util.browser import DID_NOT_FINISH, send
from util.fake_openai import FakeLocalGPTServer, FakeModel, ToolEchoModel
from util.fake_openai.echo_model import text_of
from util.mcp_server import mcp_server

pytestmark = [pytest.mark.io, pytest.mark.database]

# What the retry is told the failed answer did (worker/failures.py).
POISON_CALL = '- shout({"text": "poison pill"}) -> result, tool output: "POISON PILL"'


@pytest.fixture
def release() -> threading.Event:
    return threading.Event()


@pytest.fixture
def fake_model() -> FakeModel:
    return ToolEchoModel("shout")


@pytest.fixture
def tools_url(release: threading.Event) -> Iterator[str]:
    def shout(text: str) -> str:
        """The text, in capitals."""
        if text == "hold":
            release.wait(30)
        return text.upper()

    with mcp_server(shout) as url:
        yield url


def last_user_text(request: dict[str, Any]) -> str:
    return text_of(next(m for m in reversed(request["messages"]) if m["role"] == "user"))


def expect_shout_used(answer: Locator, calls: int = 1) -> None:
    """The answer's calls are folded under "N tool call(s)"; unfolded, each is "Used tool:
    shout"."""
    answer.get_by_role("button", name=f"{calls} tool call{'s' if calls > 1 else ''}").click()
    expect(answer.locator('[data-slot="tool-fallback-trigger"]')).to_have_text(
        ["Used tool: shout"] * calls
    )


def aux_tool_calls(
    agent: str, server: str, page: Page, local_gpt: FakeLocalGPTServer, release: threading.Event
) -> None:
    calls = local_gpt.received
    answers = page.locator('[data-role="assistant"]')

    # 1. "New chat", pick the agent with tools, send "hello": the model calls "shout", the MCP
    #    server runs it, and the model answers with its result. The answer shows the call under
    #    "1 tool call" as "Used tool: shout"; opening that shows the arguments and the "Result:".
    page.goto(server)
    page.get_by_role("button", name="New chat").click()
    page.get_by_label("Agent").select_option(f"{agent}_tools")
    send(page, "hello")
    expect(answers.first).to_contain_text("The tool said: HELLO")
    expect_shout_used(answers.first)
    answers.first.locator('[data-slot="tool-fallback-trigger"]').click()
    expect(answers.first.locator('[data-slot="tool-fallback-args"]')).to_contain_text("hello")
    expect(answers.first.locator('[data-slot="tool-fallback-result"]')).to_contain_text("HELLO")
    assert len(calls) == 2
    assert calls[-1]["messages"][-1]["role"] == "tool"
    assert text_of(calls[-1]["messages"][-1]) == "HELLO"

    # 2. Reload: the tool call and the answer were stored.
    page.reload()
    expect(answers.first).to_contain_text("The tool said: HELLO")
    expect_shout_used(answers.first)

    # 3. Send "poison pill": the tool runs, then the model server fails the call that brings
    #    its result. The failed answer shows the call it made, and that it did not finish.
    send(page, "poison pill")
    expect(answers).to_have_count(2)
    expect(answers.last.get_by_text(DID_NOT_FINISH)).to_be_visible()
    expect_shout_used(answers.last)
    assert len(calls) == 4

    # 4. Reload: the failed answer keeps its tool call and its failure.
    page.reload()
    expect(answers.last.get_by_text(DID_NOT_FINISH)).to_be_visible()
    expect_shout_used(answers.last)
    expect(page.get_by_text(DID_NOT_FINISH)).to_have_count(1)

    # 5. Retry it ("Refresh" on the failed answer): the model is told what the failed answer
    #    did, the tool call and its output quoted, and the retry fails again the same way.
    #    The old failed answer is on the screen until the new one replaces it, so wait for
    #    the new one's id before reading its failure.
    first_try = answers.last.get_attribute("data-message-id")
    answers.last.hover()
    answers.last.get_by_role("button", name="Refresh").click()
    expect(answers.last).not_to_have_attribute("data-message-id", first_try or "")
    expect(answers).to_have_count(2)
    expect(answers.last.get_by_text(DID_NOT_FINISH)).to_be_visible()
    assert len(calls) == 6
    retry = last_user_text(calls[-2])
    assert retry.startswith("poison pill"), retry
    assert POISON_CALL in retry, retry

    # 6. Send "hello then hold": two rounds in one answer. The second call shows while its tool
    #    still runs, beside the first and its result; then the answer ends on what it returned.
    send(page, "hello then hold")
    try:
        expect(answers).to_have_count(3)
        expect(answers.last.get_by_role("button", name="2 tool calls")).to_be_visible()
    finally:
        release.set()
    expect(answers.last).to_contain_text("The tool said: HOLD")
    expect_shout_used(answers.last, calls=2)

    # 7. Reload: both calls were stored on the one answer.
    page.reload()
    expect(answers.last).to_contain_text("The tool said: HOLD")
    expect_shout_used(answers.last, calls=2)


def test_pydantic_ai_tool_calls(
    server: str, page: Page, local_gpt: FakeLocalGPTServer, release: threading.Event
) -> None:
    aux_tool_calls("pydantic_ai", server, page, local_gpt, release)


def test_langchain_tool_calls(
    server: str, page: Page, local_gpt: FakeLocalGPTServer, release: threading.Event
) -> None:
    aux_tool_calls("langchain", server, page, local_gpt, release)
