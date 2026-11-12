# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A turn whose tool results add up to several times its model's window still finishes, in
both engines: LangChain summarises the older messages, Pydantic AI clears older tool results
and cuts one too large. The model server refuses a prompt larger than the window, as a vendor
does."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from util import stack
from util.controller_db import requires_postgres
from util.fake_openai import CallTool, FakeLocalGPTServer
from util.fake_openai.echo_model import text_of
from util.mcp_server import mcp_server

pytestmark = requires_postgres

WINDOW = 4000
"""Tokens: the configured window of the model."""

ROUNDS = 10

PAGE = "a page of text " * 200
"""About 3,000 characters: ten of them are several windows."""

SMALL = f"""
[models.small_gpt]
provider = "local_gpt"
name = "fake-gpt-small"
title = "Small GPT"
context_window = {WINDOW}

[tool_servers.pages]
url = "{{tools_url}}"
auth = "none"

[agents.langchain_pages]
title = "LangChain, reading"
system_prompt = "{{system_prompt}}"
models = ["small_gpt"]
engine = "langchain"
tools = ["pages"]

[agents.pydantic_ai_pages]
title = "Pydantic AI, reading"
system_prompt = "{{system_prompt}}"
models = ["small_gpt"]
engine = "pydantic-ai"
tools = ["pages"]
"""


def read(n: int) -> str:
    """The page ``n``; the first is ten pages long."""
    return PAGE * 10 if n == 0 else PAGE


class SmallWindowModel:
    """Reads ten pages, one a round, then answers "done". Refuses a prompt larger than the
    window, at four characters a token; a request to summarise is answered with a summary."""

    def __init__(self) -> None:
        self.read = 0
        self.largest = 0

    def reply(self, messages: list[dict[str, Any]]) -> str | CallTool:
        size = len(json.dumps(messages))
        self.largest = max(self.largest, size)
        if size > WINDOW * 4:
            raise RuntimeError(f"prompt is too long: {size} characters")
        if "<role>\nContext Extraction Assistant" in text_of(messages[-1]):
            return "Read some pages so far."
        if self.read < ROUNDS:
            self.read += 1
            return CallTool("read", {"n": self.read - 1}, call_id=f"call_{self.read}")
        return "done"


@pytest.fixture(scope="module")
def small_window(
    model_server: FakeLocalGPTServer, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[str]:
    with mcp_server(read) as tools_url:
        config = tmp_path_factory.mktemp("small") / "robinauts.toml"
        small = SMALL.format(tools_url=tools_url, system_prompt=stack.SYSTEM_PROMPT)
        config.write_text(stack.config_for(model_server.base_url) + small)
        with (
            stack.database(config) as url,
            stack.server(config, url, env=stack.API_KEY) as server,
        ):
            yield server


@pytest.mark.parametrize("agent", ["langchain_pages", "pydantic_ai_pages"])
def test_a_turn_larger_than_its_window_finishes(
    small_window: str, model_server: FakeLocalGPTServer, agent: str
) -> None:
    model = SmallWindowModel()
    model_server.model = model

    with httpx.Client(base_url=small_window, timeout=60.0) as api:
        started = api.post("/api/turns", json={"agent_id": agent, "text": "read"})

    assert started.status_code == 200, started.text
    last = [line for line in started.text.splitlines() if line.startswith("data: ")][-1]
    assert '"RUN_FINISHED"' in last, last
    assert model.read == ROUNDS
    assert model.largest <= WINDOW * 4
