# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A failed answer, through each engine, says so in its place and keeps saying it.

The model server fails the call, through ``PoisonEchoModel``, so the failure goes through a real
engine's error handling.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from util.browser import DID_NOT_FINISH, send
from util.fake_openai import FakeLocalGPTServer, FakeModel, PoisonEchoModel
from util.stack import sent

pytestmark = [pytest.mark.io, pytest.mark.database]

RETRIED = "The user was told it failed, and pressed the retry button."


@pytest.fixture
def fake_model() -> FakeModel:
    return PoisonEchoModel()


def aux_failed_answer(agent: str, server: str, page: Page, local_gpt: FakeLocalGPTServer) -> None:
    calls = local_gpt.received

    # 1. "New chat", pick the agent, send "poison": the model server answers 400, and the
    #    answer's place says it did not finish, once.
    page.goto(server)
    page.get_by_role("button", name="New chat").click()
    page.get_by_label("Agent").select_option(agent)
    send(page, "poison")
    answers = page.locator('[data-role="assistant"]')
    failed = answers.first.get_by_text(DID_NOT_FINISH)
    expect(failed).to_be_visible()
    expect(page.get_by_text(DID_NOT_FINISH)).to_have_count(1)
    assert sent(calls[-1]) == [("user", "poison")]

    # 2. Reload: the question and the failure come back from the database.
    page.reload()
    expect(page.locator('[data-role="user"]')).to_contain_text("poison")
    expect(failed).to_be_visible()
    expect(page.get_by_text(DID_NOT_FINISH)).to_have_count(1)

    # 3. Retry is the "Refresh" button of a failed answer: a fresh answer to the same question,
    #    which fails again. A retry, not a regeneration: the model is told about the failure.
    #    The engine remembers nothing of the failed turn, so that note is all it is sent.
    first_try = answers.first.get_attribute("data-message-id")
    answers.first.hover()
    page.get_by_role("button", name="Refresh").click()
    expect(answers.first).not_to_have_attribute("data-message-id", first_try or "")
    expect(answers).to_have_count(1)
    expect(failed).to_be_visible()
    expect(page.get_by_text(DID_NOT_FINISH)).to_have_count(1)
    retry = sent(calls[-1])
    assert [role for role, _ in retry] == ["user"], retry
    assert retry[0][1].startswith("poison"), retry
    assert RETRIED in retry[0][1], retry

    # 4. Send "hello": it goes under the failed answer, which stays and still says it failed.
    #    The engine remembers nothing of the failed turns, so the model gets one message: the
    #    failed exchange, then the new message, which it echoes.
    send(page, "hello")
    expect(answers).to_have_count(2)
    expect(answers.last).to_contain_text("Message: poison")
    expect(answers.last).to_contain_text("The user's new message:")
    expect(answers.last).to_contain_text("hello")
    expect(failed).to_be_visible()
    reply = sent(calls[-1])
    assert [role for role, _ in reply] == ["user"], reply
    assert "Message: poison" in reply[0][1], reply
    assert reply[0][1].endswith("hello"), reply

    # 5. Reload: the answer and the failure above it are both still there.
    page.reload()
    expect(answers).to_have_count(2)
    expect(answers.last).to_contain_text("hello")
    expect(failed).to_be_visible()
    expect(page.get_by_text(DID_NOT_FINISH)).to_have_count(1)
    assert len(calls) == 3


def test_pydantic_ai_failed_answer(server: str, page: Page, local_gpt: FakeLocalGPTServer) -> None:
    aux_failed_answer("pydantic_ai", server, page, local_gpt)


def test_langchain_failed_answer(server: str, page: Page, local_gpt: FakeLocalGPTServer) -> None:
    aux_failed_answer("langchain", server, page, local_gpt)


def test_kataloop_failed_answer(server: str, page: Page, local_gpt: FakeLocalGPTServer) -> None:
    aux_failed_answer("kataloop", server, page, local_gpt)
