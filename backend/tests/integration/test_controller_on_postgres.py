# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The composition on PostgreSQL: `db init`, the start-up check, and a turn over the store."""

from __future__ import annotations

import asyncio
import hashlib
import pathlib
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import pytest

from aio import asyncio_test
from controller_db import requires_postgres, temporary_schema, url
from robinauts.agent_engines.contract.domain import Event
from robinauts.agent_engines.echo_engine.engine import EchoEngine
from robinauts.controller.composition import SCHEMA_READY, compose, init_database
from robinauts.controller.contract.domain import (
    AgentConfig,
    Config,
    ConfigError,
    Identity,
    ModelConfig,
    ProviderConfig,
    ProviderKind,
    SessionNotFoundError,
    StorageConfig,
    StorageKind,
    TextPart,
    TurnEnded,
    TurnState,
    UserSession,
)
from robinauts.web.cli import run

pytestmark = requires_postgres

CONFIG = Config(
    providers={"echo": ProviderConfig("echo", ProviderKind.ANTHROPIC, "ECHO_API_KEY")},
    models={"echo": ModelConfig("echo", provider="echo", name="echo", title="Echo")},
    agents={
        "echo": AgentConfig("echo", title="Echo", system_prompt="", model="echo", engine="echo")
    },
)

EXAMPLE = pathlib.Path(__file__).resolve().parents[3] / "examples" / "echo.toml"


def dsn_in(schema_name: str) -> str:
    """The test database, with the search path on this test's schema."""
    joiner = "&" if "?" in url() else "?"
    return f"{url()}{joiner}search_path={schema_name}"


@asyncio_test
async def test_start_refuses_a_database_with_no_schema_and_serves_after_db_init() -> None:
    async with temporary_schema(applied=False) as schema:
        dsn = dsn_in(schema.name)
        storage = StorageConfig(StorageKind.POSTGRES, url=dsn)
        composed = compose(CONFIG, storage=storage, secret_for={}.get)
        controller = composed.controller
        with pytest.raises(ConfigError, match="no schema: run `robinauts db init`"):
            await controller.open()
        said = await init_database(dsn, CONFIG, {}.get)
        assert said.startswith(SCHEMA_READY.split("{")[0])
        assert await init_database(dsn, CONFIG, {}.get) == said

        await controller.open()
        user = await controller.ensure_user(Identity("local", "me"))
        started = await controller.start_session(user, agent="echo", model="echo", text="hello")
        async for _ in controller.watch_turn(user, started.session_id, started.turn_id):
            pass
        opened = await controller.open_session(user, started.session_id)
        assert [m.role.value for m in opened.messages] == ["user", "assistant"]
        assert opened.messages[1].parts[-1] == TextPart("The tool said: hello")
        assert opened.active is None
        page = await controller.list_sessions(user, limit=10)
        assert [s.id for s in page.sessions] == [started.session_id]

        # The credentials are on the pool the controller opened.
        now = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
        cookie = hashlib.sha256(b"cookie").hexdigest()
        signed_in = UserSession(uuid.uuid4(), user.id, cookie, now, now + timedelta(hours=1))
        await composed.credentials.add_user_session(signed_in)
        assert await composed.credentials.resolve_user_session(cookie, now) == user
        await controller.close()


def alone(statement: str) -> None:
    async def go() -> None:
        connection = await asyncpg.connect(url())
        try:
            await connection.execute(statement)
        finally:
            await connection.close()

    asyncio.run(go())


def test_the_command_initialises_and_says_so_twice(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    name = "robinauts_test_" + uuid.uuid4().hex
    alone(f'CREATE SCHEMA "{name}"')
    try:
        monkeypatch.setenv("ROBINAUTS_CONFIG", str(EXAMPLE))
        monkeypatch.setenv("ROBINAUTS_DATABASE_URL", dsn_in(name))
        run(["db", "init"])
        first = capsys.readouterr().out.strip()
        assert first.startswith("the database is at schema version 1")
        run(["db", "init"])
        assert capsys.readouterr().out.strip() == first
    finally:
        alone(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')


def test_db_init_without_a_database_url_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ROBINAUTS_DATABASE_URL", raising=False)
    with pytest.raises(SystemExit) as left:
        run(["db", "init"])
    assert left.value.code == 2
    assert "ROBINAUTS_DATABASE_URL is not set" in capsys.readouterr().err


class Held(EchoEngine):
    """Streams nothing until it is let go."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()

    async def stream(self, *args: Any, **kwargs: Any) -> AsyncGenerator[Event, None]:
        await self.gate.wait()
        async for event in super().stream(*args, **kwargs):
            yield event


@asyncio_test
async def test_a_turn_running_in_one_process_is_stopped_and_deleted_through_another() -> None:
    async with temporary_schema() as schema:
        storage = StorageConfig(StorageKind.POSTGRES, url=dsn_in(schema.name))
        first = compose(CONFIG, storage=storage, secret_for={}.get).controller
        second = compose(CONFIG, storage=storage, secret_for={}.get).controller
        await first.open()
        await second.open()
        try:
            first._engines["echo"] = Held()
            user = await first.ensure_user(Identity("local", "me"))
            for ask in ("stop", "delete"):
                started = await first.start_session(user, agent="echo", model="echo", text=ask)
                sid, tid = started.session_id, started.turn_id
                await first._store.wait_for_events(user.id, sid, tid, 0, 5.0)
                if ask == "stop":
                    await second.cancel_turn(user, sid, tid)
                    watched = [e async for e in second.watch_turn(user, sid, tid)]
                    assert watched[-1].event == TurnEnded(TurnState.CANCELLED)
                else:
                    await second.delete_session(user, sid)
                    with pytest.raises(SessionNotFoundError):
                        await first.open_session(user, sid)
                    # The process running it was told, and its runner is gone.
                    for _ in range(100):
                        if not first._dispatcher.running():
                            break
                        await asyncio.sleep(0.05)
                    assert first._dispatcher.running() == []
        finally:
            await first.close()
            await second.close()
