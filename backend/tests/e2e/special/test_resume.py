# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Resume, on a failed answer, through each engine: the turn goes on from the tool rounds its
engine saved, and none of them runs again."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from playwright.sync_api import Page, expect

from util.browser import DID_NOT_FINISH, send
from util.fake_openai import CallTool, FakeLocalGPTServer, FakeModel
from util.mcp_server import mcp_server

pytestmark = [pytest.mark.io, pytest.mark.database]

counted: list[int] = []


def count(n: int) -> str:
    """Counts ``n``."""
    counted.append(n)
    return f"counted {n}"


class RefusedOnce:
    """Calls ``count`` three times, then answers "done"; refused for good before the third
    call, the first time only."""

    def __init__(self) -> None:
        self.refused = False

    def reply(self, messages: list[dict[str, Any]]) -> str | CallTool:
        rounds = sum(m["role"] == "tool" for m in messages)
        if rounds == 2 and not self.refused:
            self.refused = True
            raise RuntimeError("refused")
        if rounds < 3:
            return CallTool("count", {"n": rounds}, call_id=f"call_{rounds}")
        return "done"


@pytest.fixture
def fake_model() -> FakeModel:
    return RefusedOnce()


@pytest.fixture
def tools_url() -> Iterator[str]:
    counted.clear()
    with mcp_server(count) as url:
        yield url


def aux_resume(agent: str, server: str, page: Page, local_gpt: FakeLocalGPTServer) -> None:
    # 1. Two tool rounds, then the model server refuses: the answer did not finish.
    page.goto(server)
    page.get_by_role("button", name="New chat").click()
    page.get_by_label("Agent").select_option(agent)
    send(page, "go")
    answers = page.locator('[data-role="assistant"]')
    expect(answers.last.get_by_text(DID_NOT_FINISH)).to_be_visible()
    assert counted == [0, 1]

    # 2. Resume: the turn goes on from its two rounds, and finishes.
    answers.last.hover()
    answers.last.get_by_role("button", name="Resume").click()
    expect(answers.last).to_contain_text("done")
    expect(page.get_by_text(DID_NOT_FINISH)).to_have_count(0)
    assert counted == [0, 1, 2]

    # 3. Reload: the finished answer is what the conversation keeps.
    page.reload()
    expect(answers.last).to_contain_text("done")
    expect(page.get_by_text(DID_NOT_FINISH)).to_have_count(0)


def test_pydantic_ai_resume(server: str, page: Page, local_gpt: FakeLocalGPTServer) -> None:
    aux_resume("pydantic_ai_tools", server, page, local_gpt)


def test_langchain_resume(server: str, page: Page, local_gpt: FakeLocalGPTServer) -> None:
    aux_resume("langchain_tools", server, page, local_gpt)


def test_kataloop_resume(server: str, page: Page, local_gpt: FakeLocalGPTServer) -> None:
    aux_resume("kataloop_tools", server, page, local_gpt)
