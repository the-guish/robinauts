# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The Pydantic AI engine keeps the engines' contract over local storage, a SQLite file in the
worker's folder, and its sessions outlive the engine that wrote them."""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, UserPromptPart
from pydantic_ai.models.test import TestModel
from pydantic_ai.toolsets import FunctionToolset
from test_pydanticai_engine import scripted

from robinauts.agent_engines.contract.domain import ProviderKind, StorageRefusedError
from robinauts.agent_engines.contract.ports import AgentEngine, StorageConfig, StorageKind
from robinauts.agent_engines.pydantic_ai_engine import engine as engine_module
from robinauts.agent_engines.pydantic_ai_engine import init_pydantic_ai
from robinauts.agent_engines.pydantic_ai_engine.engine import PydanticAIEngine
from robinauts.agent_engines.pydantic_ai_engine.session_store import (
    FILE_NAME,
    SQLiteSessionStore,
)
from util.contracts.engine import EngineSessionContract, EngineTurnContract, Script, add, turn
from util.engine_settings import settings_for


def on_sqlite(folder: Path) -> PydanticAIEngine:
    return PydanticAIEngine(settings_for(ProviderKind.ANTHROPIC), SQLiteSessionStore(folder))


class OnSQLite:
    """An engine over a folder of its own per call, all closed when the test ends."""

    @pytest.fixture(autouse=True)
    def folders(self, tmp_path: Path) -> None:
        self.root = tmp_path

    async def engine_on_sqlite(self) -> PydanticAIEngine:
        engine = on_sqlite(self.root / str(uuid.uuid4()))
        self.__dict__.setdefault("engines", []).append(engine)
        await engine.setup()
        return engine

    async def release(self) -> None:
        for engine in self.__dict__.pop("engines", []):
            await engine.close()


@pytest.fixture
def answering(monkeypatch: pytest.MonkeyPatch) -> None:
    model = TestModel(custom_output_text="An answer.")
    monkeypatch.setattr(engine_module, "chat_model", lambda *_: (model, {}))


@pytest.mark.usefixtures("answering")
class TestPydanticAIEngineSessionsOnSQLite(OnSQLite, EngineSessionContract):
    async def new_engine(self) -> AgentEngine:
        return await self.engine_on_sqlite()


class TestPydanticAIEngineTurnOnSQLite(OnSQLite, EngineTurnContract):
    @pytest.fixture(autouse=True)
    def plain_tool(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def toolsets_for(*_: object) -> list[Any]:
            return [FunctionToolset([add])]

        monkeypatch.setattr(engine_module, "toolsets_for", toolsets_for)
        self.monkeypatch = monkeypatch

    async def new_engine(self, script: Script) -> AgentEngine:
        model = scripted(script)
        self.monkeypatch.setattr(engine_module, "chat_model", lambda *_: (model, {}))
        return await self.engine_on_sqlite()


def asked(text: str) -> list[ModelMessage]:
    return [ModelRequest(parts=[UserPromptPart(content=text)])]


def test_the_latest_save_is_the_last_one_even_in_place_of_an_earlier(tmp_path: Path) -> None:
    async def go() -> None:
        store = SQLiteSessionStore(tmp_path)
        await store.setup()
        try:
            session = uuid.uuid4()
            one, two, three = asked("one"), asked("two"), asked("three")
            await store.create(session)
            assert await store.latest(session) is None
            await store.save(session, "a", one)
            await store.save(session, "b", two)
            assert await store.latest(session) == ("b", two)
            await store.save(session, "a", three)
            assert await store.latest(session) == ("a", three)
            assert await store.history(session, "b") == two
            assert await store.history(session, "c") is None
        finally:
            await store.close()

    asyncio.run(go())


@pytest.mark.usefixtures("answering")
def test_init_pydantic_ai_keeps_sessions_in_the_folder_across_engines(tmp_path: Path) -> None:
    folder = tmp_path / "workers" / "default"
    storage = StorageConfig(StorageKind.LOCAL, {"path": folder})
    settings = settings_for(ProviderKind.ANTHROPIC)

    async def go() -> None:
        first = init_pydantic_ai(settings, storage)
        assert isinstance(first, PydanticAIEngine)
        await first.setup()
        session, forgotten = uuid.uuid4(), uuid.uuid4()
        await first.create(session)
        await first.create(forgotten)
        done = await turn(first, session, "m")
        await turn(first, forgotten, "m")
        await first.forget(forgotten)
        await first.close()
        assert (folder / FILE_NAME).is_file()

        second = init_pydantic_ai(settings, storage)
        await second.setup()
        try:
            assert await second.exists(session)
            assert not await second.exists(forgotten)
            await turn(second, session, "m", done.checkpoint_id)
        finally:
            await second.close()
        with sqlite3.connect(folder / FILE_NAME) as connection:
            rows = connection.execute("SELECT count(*) FROM pydantic_ai_checkpoints").fetchone()
        connection.close()
        assert rows == (2,)

    asyncio.run(go())


def test_a_file_of_another_version_is_refused(tmp_path: Path) -> None:
    async def made() -> None:
        engine = on_sqlite(tmp_path)
        await engine.setup()
        await engine.close()

    asyncio.run(made())
    with sqlite3.connect(tmp_path / FILE_NAME) as connection:
        connection.execute("UPDATE schema_version SET version = 2")
    connection.close()

    async def refused() -> None:
        engine = on_sqlite(tmp_path)
        with pytest.raises(StorageRefusedError, match="schema version 2"):
            await engine.setup()
        await engine.close()

    asyncio.run(refused())
