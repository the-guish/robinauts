# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A turn that ended early is resumed from the rounds it saved, in every engine: each on its
memory on PostgreSQL, a model server over HTTP and a real MCP server. kataloop's memory is in
this process whatever the storage, until ``kataloop[postgres]``: the test keeps one engine, so it
holds what the turn saved all the same."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from robinauts.agent_engines.contract.domain import (
    AgentDefinition,
    Done,
    Event,
    ModelConfig,
    ModelProviderConfig,
    ModelsConfig,
    ProviderKind,
    ToolServerAuth,
    ToolServerConfig,
)
from robinauts.agent_engines.contract.ports import (
    AgentEngine,
    EngineSettings,
    StorageConfig,
    StorageKind,
)
from robinauts.agent_engines.kataloop_engine import init_kataloop
from robinauts.agent_engines.langchain_engine.engine import LangChainEngine
from robinauts.agent_engines.langchain_engine.memory import PostgresMemory as LangChainMemory
from robinauts.agent_engines.pydantic_ai_engine.engine import PydanticAIEngine
from robinauts.agent_engines.pydantic_ai_engine.memory import PostgresMemory as PydanticAIMemory
from util.aio import asyncio_test
from util.controller_db import requires_postgres, temporary_schema
from util.engine_settings import Keys, NoSecrets
from util.fake_openai import CallTool, FakeLocalGPTServer
from util.fake_openai.echo_model import last_question, text_of
from util.mcp_server import mcp_server

pytestmark = requires_postgres

AGENT = AgentDefinition("You are a robinaut.", tools=("t",))
ROUNDS = 3

counted: list[int] = []


def count(n: int) -> str:
    """Counts ``n``."""
    counted.append(n)
    return f"counted {n}"


class CountingModel:
    """Answers "hello" with "hi". Asked anything else, calls ``count`` three times, then answers
    "done"; refused before the third call while ``failing``."""

    def __init__(self) -> None:
        self.failing = True

    def reply(self, messages: list[dict[str, Any]]) -> str | CallTool:
        if last_question(messages) == "hello":
            return "hi"
        asked = max(i for i, m in enumerate(messages) if m["role"] == "user")
        rounds = sum(m["role"] == "tool" for m in messages[asked:])
        if rounds == ROUNDS:
            return "done"
        if rounds == 2 and self.failing:
            raise RuntimeError("refused")
        return CallTool("count", {"n": rounds}, call_id=f"call_{rounds}")


@pytest.fixture(scope="module")
def tools_url() -> Iterator[str]:
    with mcp_server(count) as url:
        yield url


@pytest.fixture
def model_server() -> Iterator[FakeLocalGPTServer]:
    counted.clear()
    with FakeLocalGPTServer(CountingModel()) as running:
        yield running


def settings(model_url: str, tools_url: str) -> EngineSettings:
    provider = ModelProviderConfig(
        id="p", kind=ProviderKind.OPENAI_COMPATIBLE, api_key_env="", base_url=model_url
    )
    return EngineSettings(
        models=ModelsConfig(
            providers={"p": provider},
            models={"m": ModelConfig(id="m", provider="p", name="fake-gpt")},
            tool_servers={"t": ToolServerConfig(id="t", url=tools_url, auth=ToolServerAuth.NONE)},
        ),
        keys=Keys(),
        tool_secrets=NoSecrets(),
    )


Build = Callable[[EngineSettings, Any], AgentEngine]

ENGINES = pytest.mark.parametrize(
    "build",
    [
        lambda s, pool: LangChainEngine(s, LangChainMemory(pool)),
        lambda s, pool: PydanticAIEngine(s, PydanticAIMemory(pool)),
        lambda s, pool: init_kataloop(s, StorageConfig(StorageKind.POSTGRES, {"pool": pool})),
    ],
    ids=["langchain", "pydantic-ai", "kataloop"],
)


async def run(
    engine: AgentEngine,
    session: uuid.UUID,
    prompt: str,
    after: str | None,
    resume: bool = False,
) -> list[Event]:
    stream = engine.stream(
        session,
        AGENT,
        prompt,
        model="m",
        checkpoint_id=after,
        timeout_seconds=30.0,
        resume=resume,
    )
    return [event async for event in stream]


@ENGINES
@asyncio_test
async def test_a_resumed_turn_goes_on_from_the_rounds_it_saved(
    build: Build, model_server: FakeLocalGPTServer, tools_url: str
) -> None:
    model: Any = model_server.model
    async with temporary_schema(applied=False, size=2) as schema:
        engine = build(settings(model_server.base_url, tools_url), schema.pool)
        await engine.setup()
        session = uuid.uuid4()
        await engine.create(session)
        first = (await run(engine, session, "hello", None))[-1]
        assert isinstance(first, Done)

        # Refused after two rounds: the turn fails with two tool calls made.
        with pytest.raises(Exception, match="refused"):
            await run(engine, session, "go", first.checkpoint_id)
        assert counted == [0, 1]

        model.failing = False
        sent = len(model_server.received)
        resumed = await run(engine, session, "go", first.checkpoint_id, resume=True)
        done = resumed[-1]
        assert isinstance(done, Done)
        assert done.text == "done"
        assert counted == [0, 1, 2]
        # Its first request carried the two results it had, the question once.
        request = model_server.received[sent]["messages"]
        assert [m["role"] for m in request].count("tool") == 2
        assert [text_of(m) for m in request if m["role"] == "user"] == ["hello", "go"]

        # Finished, it is the same answer again: the caller had not stored it.
        sent = len(model_server.received)
        again = await run(engine, session, "go", first.checkpoint_id, resume=True)
        assert again == [done]
        assert len(model_server.received) == sent

        # A session that kept nothing runs the turn from the start.
        fresh = uuid.uuid4()
        await engine.create(fresh)
        counted.clear()
        started = await run(engine, fresh, "go", None, resume=True)
        assert started[-1] == Done(text="done", checkpoint_id=started[-1].checkpoint_id)
        assert counted == [0, 1, 2]
