# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The LangChain engine keeps the engines' contract over PostgreSQL storage, and its saver
holds what the in-memory one would, checkpoint for checkpoint."""

from __future__ import annotations

import itertools
import uuid
from typing import Any

import pytest
from langchain_core.language_models import GenericFakeChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from test_langchain_engine import ScriptedChatModel, settings_for

from aio import asyncio_test
from contracts.engine import AGENT, EngineMemoryContract, EngineTurnContract, Script, add
from controller_db import TemporarySchema, requires_postgres, temporary_schema
from robinauts.agent_engines.contract.domain import Done, ProviderKind
from robinauts.agent_engines.contract.ports import AgentEngine, StorageConfig, StorageKind
from robinauts.agent_engines.langchain_engine import engine as engine_module
from robinauts.agent_engines.langchain_engine import init_langchain
from robinauts.agent_engines.langchain_engine.engine import LangChainEngine
from robinauts.agent_engines.langchain_engine.saver import (
    PostgresSaver,
    PostgresSessions,
    Sessions,
)

pytestmark = requires_postgres


def on_postgres(pool: Any) -> LangChainEngine:
    return LangChainEngine(
        settings_for(ProviderKind.ANTHROPIC), PostgresSaver(pool), PostgresSessions(pool)
    )


def in_memory() -> LangChainEngine:
    return LangChainEngine(settings_for(ProviderKind.ANTHROPIC), InMemorySaver(), Sessions())


class OnPostgres:
    async def engine_on_postgres(self) -> LangChainEngine:
        schema = TemporarySchema(size=2)
        pool = await schema.open()
        self.__dict__.setdefault("schemas", []).append(schema)
        engine = on_postgres(pool)
        await engine.setup()
        return engine

    async def release(self) -> None:
        for schema in self.__dict__.pop("schemas", []):
            await schema.close()


class TestLangChainEngineMemoryOnPostgres(OnPostgres, EngineMemoryContract):
    @pytest.fixture(autouse=True)
    def scripted_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        model = GenericFakeChatModel(messages=itertools.repeat("An answer."))
        monkeypatch.setattr(engine_module, "chat_model", lambda *_: model)

    async def new_engine(self) -> AgentEngine:
        return await self.engine_on_postgres()


class TestLangChainEngineTurnOnPostgres(OnPostgres, EngineTurnContract):
    @pytest.fixture(autouse=True)
    def plain_tool(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def tools_for(*_: object) -> list[Any]:
            return [add]

        monkeypatch.setattr(engine_module, "tools_for", tools_for)
        self.monkeypatch = monkeypatch

    async def new_engine(self, script: Script) -> AgentEngine:
        model = ScriptedChatModel(script=script)
        self.monkeypatch.setattr(engine_module, "chat_model", lambda *_: model)
        return await self.engine_on_postgres()


async def checkpoints_of(saver: BaseCheckpointSaver[str], session: uuid.UUID) -> list[Any]:
    """Each checkpoint of the thread, oldest first: its source, its step, and its messages."""
    found = []
    async for each in saver.alist({"configurable": {"thread_id": str(session)}}):
        messages = each.checkpoint["channel_values"].get("messages", [])
        found.append(
            (
                each.metadata.get("source"),
                each.metadata.get("step"),
                [(type(m).__name__, m.text, len(getattr(m, "tool_calls", []))) for m in messages],
                each.parent_config is not None,
            )
        )
    return list(reversed(found))


@pytest.mark.parametrize("script", [Script.ANSWER, Script.TOOL_ROUND])
def test_the_saver_holds_what_the_in_memory_one_would(
    monkeypatch: pytest.MonkeyPatch, script: Script
) -> None:
    import asyncio

    async def tools_for(*_: object) -> list[Any]:
        return [add]

    monkeypatch.setattr(engine_module, "tools_for", tools_for)
    monkeypatch.setattr(engine_module, "chat_model", lambda *_: ScriptedChatModel(script=script))

    async def run(engine: LangChainEngine) -> tuple[Done, list[Any]]:
        session = uuid.uuid4()
        await engine.create(session)
        events = [
            e
            async for e in engine.stream(
                session, AGENT, "hello", model="m", checkpoint_id=None, timeout_seconds=10.0
            )
        ]
        done = events[-1]
        assert isinstance(done, Done)
        return done, await checkpoints_of(engine._saver, session)

    async def both() -> None:
        in_process = in_memory()
        await in_process.setup()
        done_in_memory, saved_in_memory = await run(in_process)
        async with temporary_schema(applied=False, size=2) as schema:
            engine = on_postgres(schema.pool)
            await engine.setup()
            done_on_postgres, saved_on_postgres = await run(engine)
        assert done_on_postgres.text == done_in_memory.text
        assert saved_on_postgres == saved_in_memory
        assert len(saved_on_postgres) >= 2

    asyncio.run(both())


@asyncio_test
async def test_init_langchain_puts_memory_on_postgres_when_asked() -> None:
    async with temporary_schema(applied=False, size=2) as schema:
        engine = await init_langchain(
            settings_for(ProviderKind.ANTHROPIC),
            StorageConfig(StorageKind.POSTGRES, {"pool": schema.pool}),
        )
        assert isinstance(engine, LangChainEngine)
        assert isinstance(engine._saver, PostgresSaver)
        assert isinstance(engine._sessions, PostgresSessions)
        await engine.setup()
        session = uuid.uuid4()
        await engine.create(session)
        assert await schema.pool.fetchval("SELECT count(*) FROM langgraph_sessions") == 1
