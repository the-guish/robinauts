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
from robinauts.controller.contract.ports import Controller
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


async def released(pod: Any) -> None:
    """Until the pod's dispatcher holds nothing."""
    while pod._dispatcher.held():
        await asyncio.sleep(0.01)


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


class Gated(EchoEngine):
    """Echo, once let go: a turn that runs until the test says."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()

    async def stream(self, *args: Any, **kwargs: Any) -> AsyncGenerator[Event, None]:
        await self.gate.wait()
        async for event in super().stream(*args, **kwargs):
            yield event


async def two_pods(dsn: str) -> tuple[Controller, Controller]:
    """Two controllers on one database, as two replicas are."""
    await init_database(dsn, CONFIG, {}.get)
    storage = StorageConfig(StorageKind.POSTGRES, url=dsn)
    pods = []
    for name in ("pod-a", "pod-b"):
        pod = compose(CONFIG, storage=storage, secret_for={}.get, worker_id=name).controller
        await pod.open()
        pods.append(pod)
    return pods[0], pods[1]


@asyncio_test
async def test_a_stop_asked_of_another_pod_ends_the_turn_where_it_runs() -> None:
    async with temporary_schema(applied=False) as schema:
        a, b = await two_pods(dsn_in(schema.name))
        try:
            engine = Gated()
            a._engines["echo"] = engine
            user = await a.ensure_user(Identity("local", "me"))
            started = await a.start_session(user, agent="echo", model="echo", text="hello")
            sid = started.session_id
            assert await b.cancel_turn(user, sid, started.turn_id) is True
            turn = await b._store.get_turn(user.id, sid, started.turn_id)
            assert turn is not None
            assert turn.state is TurnState.CANCELLED
            assert turn.worker_id == "pod-a"
            assert turn.cancel_requested_at is not None
            # Its runner wrote the end; the task is gone a moment later.
            await asyncio.wait_for(released(a), 5.0)
            watched = [e.event async for e in b.watch_turn(user, sid, started.turn_id)]
            assert watched[-1] == TurnEnded(TurnState.CANCELLED)
        finally:
            await a.close()
            await b.close()


@asyncio_test
async def test_a_conversation_is_deleted_from_a_pod_that_does_not_run_its_turn() -> None:
    async with temporary_schema(applied=False) as schema:
        a, b = await two_pods(dsn_in(schema.name))
        try:
            a._engines["echo"] = Gated()
            user = await a.ensure_user(Identity("local", "me"))
            started = await a.start_session(user, agent="echo", model="echo", text="hello")
            await b.delete_session(user, started.session_id)
            with pytest.raises(SessionNotFoundError):
                await a.open_session(user, started.session_id)
            # Its runner wrote the end; the task is gone a moment later.
            await asyncio.wait_for(released(a), 5.0)
        finally:
            await a.close()
            await b.close()


@asyncio_test
async def test_a_stop_whose_signal_was_missed_is_read_back_by_the_heartbeat() -> None:
    async with temporary_schema(applied=False) as schema:
        a, b = await two_pods(dsn_in(schema.name))
        try:
            a._engines["echo"] = Gated()
            user = await a.ensure_user(Identity("local", "me"))
            started = await a.start_session(user, agent="echo", model="echo", text="hello")
            sid = started.session_id
            # Asked for in the store with no signal at all, as when the listener had dropped.
            async with b._store.pool.acquire() as connection:
                await connection.execute(
                    "UPDATE turns SET cancel_requested_at = $2 WHERE id = $1",
                    started.turn_id,
                    datetime.now(UTC),
                )
            beat = await a._work.beat()
            assert beat.cancelled == {started.turn_id}
            async for _ in b.watch_turn(user, sid, started.turn_id):
                pass
            turn = await b._store.get_turn(user.id, sid, started.turn_id)
            assert turn is not None
            assert turn.state is TurnState.CANCELLED
        finally:
            await a.close()
            await b.close()


@asyncio_test
async def test_two_pods_sweeping_at_once_end_a_turn_left_behind_once() -> None:
    async with temporary_schema(applied=False) as schema:
        a, b = await two_pods(dsn_in(schema.name))
        try:
            engine = Gated()
            a._engines["echo"] = engine
            user = await a.ensure_user(Identity("local", "me"))
            started = await a.start_session(user, agent="echo", model="echo", text="hello")
            sid = started.session_id
            async with asyncio.timeout(5.0):
                while not await a._store.last_position(user.id, sid, started.turn_id):
                    await asyncio.sleep(0.01)
            # Pod a dies: no heartbeat, no runner.
            await a._work.stop()
            a._dispatcher._tasks[started.turn_id].cancel("lost")
            later = datetime.now(UTC) + timedelta(minutes=5)
            a._now = b._now = lambda: later
            await asyncio.gather(a.sweep(), b.sweep(), a.end_left_turns(), b.end_left_turns())
            turn = await b._store.get_turn(user.id, sid, started.turn_id)
            assert turn is not None
            assert turn.state is TurnState.INTERRUPTED
            roles = [m.role.value for m in (await b.open_session(user, sid)).messages]
            assert roles == ["user", "assistant"]
        finally:
            await a.close()
            await b.close()
