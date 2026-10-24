# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A turn whose engine says nothing for ``stalled_after_seconds`` is stopped as failed; one that
keeps saying something runs on past it, in every engine."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from util import stack
from util.controller_db import requires_postgres
from util.fake_openai import CallTool, FakeLocalGPTServer
from util.fake_openai.echo_model import last_question
from util.mcp_server import mcp_server

pytestmark = requires_postgres

STALLED_AFTER = 2

NAPPING = f"""
[work]
stalled_after_seconds = {STALLED_AFTER}
heartbeat_seconds = 0.5

[tool_servers.naps]
url = "{{tools_url}}"
auth = "none"

[agents.langchain_naps]
title = "LangChain, napping"
system_prompt = "{{system_prompt}}"
model = "local_gpt"
engine = "langchain"
tools = ["naps"]

[agents.pydantic_ai_naps]
title = "Pydantic AI, napping"
system_prompt = "{{system_prompt}}"
model = "local_gpt"
engine = "pydantic-ai"
tools = ["naps"]

[agents.kataloop_naps]
title = "kataloop, napping"
system_prompt = "{{system_prompt}}"
model = "local_gpt"
engine = "kataloop"
tools = ["naps"]
"""


async def nap(seconds: float) -> str:
    """Answers after ``seconds``."""
    await asyncio.sleep(seconds)
    return "rested"


class NappingModel:
    """Asked "long", naps 10 s once. Asked "short", naps 1 s four times. Then "rested"."""

    def reply(self, messages: list[dict[str, Any]]) -> str | CallTool:
        naps = sum(m["role"] == "tool" for m in messages)
        seconds, times = (10, 1) if last_question(messages) == "long" else (1, 4)
        if naps < times:
            return CallTool("nap", {"seconds": seconds}, call_id=f"call_{naps}")
        return "rested"


@pytest.fixture(scope="module")
def napping(
    model_server: FakeLocalGPTServer, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[str]:
    with mcp_server(nap) as tools_url:
        config = tmp_path_factory.mktemp("naps") / "robinauts.toml"
        naps = NAPPING.format(tools_url=tools_url, system_prompt=stack.SYSTEM_PROMPT)
        config.write_text(stack.config_for(model_server.base_url) + naps)
        with (
            stack.database(config) as url,
            stack.server(config, url, env=stack.API_KEY) as server,
        ):
            yield server


def last_event(server: str, agent: str, text: str) -> tuple[str, float]:
    """The turn's last event, and how long it took."""
    began = time.monotonic()
    with httpx.Client(base_url=server, timeout=60.0) as api:
        started = api.post("/api/turns", json={"agent_id": agent, "text": text})
    assert started.status_code == 200, started.text
    last = [line for line in started.text.splitlines() if line.startswith("data: ")][-1]
    return last, time.monotonic() - began


@pytest.mark.parametrize("agent", ["langchain_naps", "pydantic_ai_naps", "kataloop_naps"])
def test_a_quiet_engine_is_stopped_and_a_busy_one_is_not(
    napping: str, model_server: FakeLocalGPTServer, agent: str
) -> None:
    model_server.model = NappingModel()

    # Four naps of a second: past the limit in all, never quiet for as long.
    last, _ = last_event(napping, agent, "short")
    assert '"RUN_FINISHED"' in last, last

    # A nap of ten seconds: stopped as failed, well before it ends.
    last, took = last_event(napping, agent, "long")
    assert '"code":"failed"' in last, last
    assert took < 8
