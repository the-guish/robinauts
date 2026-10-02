# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The Pydantic AI engine keeps the engines' contract over PostgreSQL storage."""

from __future__ import annotations

import uuid

import pytest
from test_pydanticai_engine import scripted, settings_for

from aio import asyncio_test
from contracts.engine import EngineMemoryContract, EngineTurnContract, Script
from controller_db import TemporarySchema, requires_postgres, temporary_schema
from robinauts.agent_engines.contract.domain import ProviderKind
from robinauts.agent_engines.contract.ports import AgentEngine, StorageConfig, StorageKind
from robinauts.agent_engines.pydantic_ai_engine import engine as engine_module
from robinauts.agent_engines.pydantic_ai_engine import init_pydantic_ai
from robinauts.agent_engines.pydantic_ai_engine.engine import PydanticAIEngine
from robinauts.agent_engines.pydantic_ai_engine.memory import PostgresMemory

pytestmark = requires_postgres


class OnPostgres:
    """An engine over a schema of its own per call, all dropped when the test ends."""

    schemas: list[TemporarySchema]

    async def engine_on_postgres(self) -> PydanticAIEngine:
        schema = TemporarySchema(size=2)
        pool = await schema.open()
        self.__dict__.setdefault("schemas", []).append(schema)
        engine = PydanticAIEngine(settings_for(ProviderKind.ANTHROPIC), PostgresMemory(pool))
        await engine.setup()
        return engine

    async def release(self) -> None:
        for schema in self.__dict__.pop("schemas", []):
            await schema.close()


class TestPydanticAIEngineMemoryOnPostgres(OnPostgres, EngineMemoryContract):
    @pytest.fixture(autouse=True)
    def scripted_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pydantic_ai.models.test import TestModel

        model = TestModel(custom_output_text="An answer.")
        monkeypatch.setattr(engine_module, "chat_model", lambda *_: (model, {}))

    async def new_engine(self) -> AgentEngine:
        return await self.engine_on_postgres()


class TestPydanticAIEngineTurnOnPostgres(OnPostgres, EngineTurnContract):
    @pytest.fixture(autouse=True)
    def plain_tool(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pydantic_ai.toolsets import FunctionToolset

        from contracts.engine import add

        async def toolsets_for(*_: object) -> list[object]:
            return [FunctionToolset([add])]

        monkeypatch.setattr(engine_module, "toolsets_for", toolsets_for)
        self.monkeypatch = monkeypatch

    async def new_engine(self, script: Script) -> AgentEngine:
        model = scripted(script)
        self.monkeypatch.setattr(engine_module, "chat_model", lambda *_: (model, {}))
        return await self.engine_on_postgres()


@asyncio_test
async def test_init_pydantic_ai_puts_memory_on_postgres_when_asked() -> None:
    async with temporary_schema(applied=False, size=2) as schema:
        engine = await init_pydantic_ai(
            settings_for(ProviderKind.ANTHROPIC),
            StorageConfig(StorageKind.POSTGRES, {"pool": schema.pool}),
        )
        assert isinstance(engine, PydanticAIEngine)
        assert isinstance(engine._memory, PostgresMemory)
        await engine.setup()
        session = uuid.uuid4()
        await engine.create(session)
        assert await schema.pool.fetchval("SELECT count(*) FROM pydantic_ai_sessions") == 1
