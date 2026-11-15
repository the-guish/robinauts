# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The LangChain engine keeps the engines' contract over local storage, a SQLite file in the
worker's folder; its saver holds what the in-process one would; and its sessions outlive the
engine that wrote them."""

from __future__ import annotations

import asyncio
import itertools
import sqlite3
import uuid
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models import GenericFakeChatModel
from test_langchain_engine import ScriptedChatModel
from test_langchain_engine_on_postgres import checkpoints_of, in_this_process

from robinauts.agent_engines.contract.domain import Done, ProviderKind, StorageRefusedError
from robinauts.agent_engines.contract.ports import AgentEngine, StorageConfig, StorageKind
from robinauts.agent_engines.langchain_engine import engine as engine_module
from robinauts.agent_engines.langchain_engine import init_langchain
from robinauts.agent_engines.langchain_engine.engine import LangChainEngine
from robinauts.agent_engines.langchain_engine.session_store import FILE_NAME, SQLiteSessionStore
from util.contracts.engine import AGENT, EngineSessionContract, EngineTurnContract, Script, add
from util.contracts.engine import turn as a_turn
from util.engine_settings import settings_for


def on_sqlite(folder: Path) -> LangChainEngine:
    return LangChainEngine(settings_for(ProviderKind.ANTHROPIC), SQLiteSessionStore(folder))


class OnSQLite:
    """An engine over a folder of its own per call, all closed when the test ends."""

    @pytest.fixture(autouse=True)
    def folders(self, tmp_path: Path) -> None:
        self.root = tmp_path

    async def engine_on_sqlite(self) -> LangChainEngine:
        engine = on_sqlite(self.root / str(uuid.uuid4()))
        self.__dict__.setdefault("engines", []).append(engine)
        await engine.setup()
        return engine

    async def release(self) -> None:
        for engine in self.__dict__.pop("engines", []):
            await engine.close()


class TestLangChainEngineSessionsOnSQLite(OnSQLite, EngineSessionContract):
    @pytest.fixture(autouse=True)
    def scripted_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        model = GenericFakeChatModel(messages=itertools.repeat("An answer."))
        monkeypatch.setattr(engine_module, "chat_model", lambda *_: model)

    async def new_engine(self) -> AgentEngine:
        return await self.engine_on_sqlite()


class TestLangChainEngineTurnOnSQLite(OnSQLite, EngineTurnContract):
    @pytest.fixture(autouse=True)
    def plain_tool(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def tools_for(*_: object) -> list[Any]:
            return [add]

        monkeypatch.setattr(engine_module, "tools_for", tools_for)
        self.monkeypatch = monkeypatch

    async def new_engine(self, script: Script) -> AgentEngine:
        model = ScriptedChatModel(script=script)
        self.monkeypatch.setattr(engine_module, "chat_model", lambda *_: model)
        return await self.engine_on_sqlite()


@pytest.fixture
def answering(monkeypatch: pytest.MonkeyPatch) -> None:
    model = GenericFakeChatModel(messages=itertools.repeat("An answer."))
    monkeypatch.setattr(engine_module, "chat_model", lambda *_: model)


@pytest.mark.parametrize("script", [Script.ANSWER, Script.TOOL_ROUND])
def test_the_saver_holds_what_the_in_process_one_would(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, script: Script
) -> None:
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
        return done, await checkpoints_of(engine._session_store.saver, session)

    async def both() -> None:
        in_process = in_this_process()
        await in_process.setup()
        done_in_process, saved_in_process = await run(in_process)
        engine = on_sqlite(tmp_path)
        await engine.setup()
        try:
            done_on_sqlite, saved_on_sqlite = await run(engine)
        finally:
            await engine.close()
        assert done_on_sqlite.text == done_in_process.text
        assert saved_on_sqlite == saved_in_process
        assert len(saved_on_sqlite) >= 2

    asyncio.run(both())


@pytest.mark.usefixtures("answering")
def test_init_langchain_keeps_sessions_in_the_folder_across_engines(tmp_path: Path) -> None:
    folder = tmp_path / "workers" / "default"
    storage = StorageConfig(StorageKind.LOCAL, {"path": folder})
    settings = settings_for(ProviderKind.ANTHROPIC)

    async def go() -> None:
        first = init_langchain(settings, storage)
        assert isinstance(first, LangChainEngine)
        await first.setup()
        session, forgotten = uuid.uuid4(), uuid.uuid4()
        await first.create(session)
        await first.create(forgotten)
        done = await a_turn(first, session, "m")
        await first.forget(forgotten)
        await first.close()
        assert (folder / FILE_NAME).is_file()
        assert (folder.stat().st_mode & 0o777, (folder / FILE_NAME).stat().st_mode & 0o777) == (
            0o700,
            0o600,
        )

        second = init_langchain(settings, storage)
        await second.setup()
        try:
            assert await second.exists(session)
            assert not await second.exists(forgotten)
            await a_turn(second, session, "m", done.checkpoint_id)
        finally:
            await second.close()

    asyncio.run(go())


@pytest.mark.parametrize(
    ("plant", "refusal"),
    [
        ("CREATE TABLE other (x);", "not this engine's"),
        ("UPDATE schema_version SET version = 2;", "schema version 2"),
        ("UPDATE schema_version SET schema_sha256 = 'x';", "another edit"),
        ("DROP TABLE langgraph_writes;", "not this engine's"),
    ],
)
def test_a_file_that_is_not_this_builds_is_refused_and_left_as_it_was(
    tmp_path: Path, plant: str, refusal: str
) -> None:
    async def made() -> None:
        engine = on_sqlite(tmp_path)
        await engine.setup()
        await engine.close()

    asyncio.run(made())
    path = tmp_path / FILE_NAME
    if plant.startswith("CREATE"):
        path.unlink()
    with sqlite3.connect(path) as connection:
        connection.executescript(plant)
    connection.close()
    before = path.read_bytes()

    async def refused() -> None:
        engine = on_sqlite(tmp_path)
        try:
            with pytest.raises(StorageRefusedError, match=refusal):
                await engine.setup()
        finally:
            await engine.close()

    asyncio.run(refused())
    assert path.read_bytes() == before


def test_a_file_that_is_not_a_database_is_refused(tmp_path: Path) -> None:
    (tmp_path / FILE_NAME).write_bytes(b"not a database, " * 64)

    async def refused() -> None:
        engine = on_sqlite(tmp_path)
        with pytest.raises(StorageRefusedError, match="could not be opened"):
            await engine.setup()
        await engine.close()

    asyncio.run(refused())
