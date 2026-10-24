# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A turn goes on through the failures a long one meets: a vendor over its rate limit, a tool
that fails again and again, and a tool that outlasts its timeout. A Pydantic AI turn that does
fail keeps the rounds it finished."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator
from typing import Any

import asyncpg
import httpx
import pytest

from util import stack
from util.controller_db import requires_postgres
from util.fake_openai import CallTool, FakeLocalGPTServer, RateLimited
from util.fake_openai.echo_model import text_of
from util.mcp_server import mcp_server

pytestmark = requires_postgres

FAILURES = 3
"""Calls of ``fail`` in a row: more than Pydantic AI's default retries allow."""

RATE_LIMITED = 5
"""Requests answered ``429`` before the first answer: more than the clients used to retry."""

FLAKY = """
[tool_servers.flaky]
url = "{tools_url}"
auth = "none"
timeout_seconds = 1

[agents.langchain_flaky]
title = "LangChain, flaky tools"
system_prompt = "{system_prompt}"
model = "local_gpt"
engine = "langchain"
tools = ["flaky"]

[agents.pydantic_ai_flaky]
title = "Pydantic AI, flaky tools"
system_prompt = "{system_prompt}"
model = "local_gpt"
engine = "pydantic-ai"
tools = ["flaky"]

[agents.kataloop_flaky]
title = "kataloop, flaky tools"
system_prompt = "{system_prompt}"
model = "local_gpt"
engine = "kataloop"
tools = ["flaky"]
"""


def fail(text: str) -> str:
    """Fails, always."""
    raise RuntimeError(f"the tool broke on {text}")


async def nap(seconds: float) -> str:
    """Answers after ``seconds``."""
    await asyncio.sleep(seconds)
    return "rested"


def shout(text: str) -> str:
    """The text, in capitals."""
    return text.upper()


def tool_results(messages: list[dict[str, Any]]) -> list[str]:
    return [text_of(m) for m in messages if m["role"] == "tool"]


class FlakyModel:
    """Over its rate limit for the first requests; then calls ``fail`` three times and ``nap``
    for longer than the tool server's timeout, and answers "done"."""

    def __init__(self) -> None:
        self.limited = 0

    def reply(self, messages: list[dict[str, Any]]) -> str | CallTool:
        if self.limited < RATE_LIMITED:
            self.limited += 1
            raise RateLimited("slow down")
        rounds = len(tool_results(messages))
        if rounds < FAILURES:
            return CallTool("fail", {"text": str(rounds)}, call_id=f"call_{rounds}")
        if rounds == FAILURES:
            return CallTool("nap", {"seconds": 5}, call_id=f"call_{rounds}")
        return "done"


class FailsAfterTwoRounds:
    """Calls ``shout`` twice, then is refused for good."""

    def reply(self, messages: list[dict[str, Any]]) -> str | CallTool:
        rounds = len(tool_results(messages))
        if rounds < 2:
            return CallTool("shout", {"text": f"round {rounds}"}, call_id=f"call_{rounds}")
        raise RuntimeError("refused")


@pytest.fixture(scope="module")
def flaky_stack(
    model_server: FakeLocalGPTServer, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[str, str]]:
    """The server's URL and its database's, on a configuration with the flaky agents."""
    with mcp_server(fail, nap, shout) as tools_url:
        config = tmp_path_factory.mktemp("flaky") / "robinauts.toml"
        flaky = FLAKY.format(tools_url=tools_url, system_prompt=stack.SYSTEM_PROMPT)
        config.write_text(stack.config_for(model_server.base_url) + flaky)
        with (
            stack.database(config) as url,
            stack.server(config, url, env=stack.API_KEY) as server,
        ):
            yield server, url


def turn(server: str, agent: str) -> httpx.Response:
    with httpx.Client(base_url=server, timeout=60.0) as api:
        started = api.post("/api/turns", json={"agent_id": agent, "text": "go"})
    assert started.status_code == 200, started.text
    return started


def last_event(started: httpx.Response) -> str:
    return [line for line in started.text.splitlines() if line.startswith("data: ")][-1]


@pytest.mark.parametrize("agent", ["langchain_flaky", "pydantic_ai_flaky", "kataloop_flaky"])
def test_a_turn_goes_on_through_rate_limits_and_failing_tools(
    flaky_stack: tuple[str, str], model_server: FakeLocalGPTServer, agent: str
) -> None:
    model_server.model = FlakyModel()
    model_server.received.clear()

    started = turn(flaky_stack[0], agent)

    assert '"RUN_FINISHED"' in last_event(started)
    assert len(model_server.received) == RATE_LIMITED + FAILURES + 2
    *failed, timed_out = tool_results(model_server.received[-1]["messages"])
    assert [f"the tool broke on {n}" in result for n, result in enumerate(failed)] == [True] * 3
    assert "Timed out" in timed_out


def test_a_failed_pydantic_ai_turn_keeps_the_rounds_it_finished(
    flaky_stack: tuple[str, str], model_server: FakeLocalGPTServer
) -> None:
    model_server.model = FailsAfterTwoRounds()
    server, database = flaky_stack

    started = turn(server, "pydantic_ai_flaky")

    assert '"RUN_ERROR"' in last_event(started)
    session = started.headers["x-robinauts-conversation-id"]
    histories = asyncio.run(saved_histories(database, session))
    returns = [
        part["content"]
        for history in histories
        for message in history
        for part in message["parts"]
        if part["part_kind"] == "tool-return"
    ]
    assert returns == ["ROUND 0", "ROUND 1"]


async def saved_histories(database: str, session: str) -> list[list[dict[str, Any]]]:
    connection = await asyncpg.connect(database)
    try:
        rows = await connection.fetch(
            "SELECT history FROM pydantic_ai_checkpoints WHERE session_id = $1", uuid.UUID(session)
        )
    finally:
        await connection.close()
    return [json.loads(row["history"]) for row in rows]
