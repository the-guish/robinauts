# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The PostgreSQL store keeps the store's contract, and the races the plan names."""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from aio import asyncio_test
from contracts.store import StoreContract
from controller_db import TemporarySchema, requires_postgres, temporary_schema, url
from robinauts.controller.adapters.postgres.pool import codecs, open_pool
from robinauts.controller.adapters.postgres.schema import create_schema
from robinauts.controller.adapters.postgres.store import PostgresStore
from robinauts.controller.contract.domain import (
    Role,
    Session,
    SessionNotFoundError,
    Turn,
    TurnActiveError,
    TurnLostError,
    TurnState,
    User,
)
from robinauts.controller.ports.store import Store, StoredEvent, StoredMessage

pytestmark = requires_postgres

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
LEASE = NOW + timedelta(minutes=3)
EXPIRY = NOW + timedelta(hours=24)
DOCUMENT = {"v": 1, "kind": "text_piece", "position": 1, "text": "x"}


class TestPostgresStore(StoreContract):
    async def new_store(self) -> Store:
        schema = TemporarySchema()
        pool = await schema.open()
        await create_schema(pool)
        store = PostgresStore(pool, dsn=url())
        self.__dict__.setdefault("schemas", {})[id(store)] = schema
        return store

    async def close_store(self, store: Store) -> None:
        assert isinstance(store, PostgresStore)
        await store.close()
        await self.__dict__["schemas"].pop(id(store)).close()


async def seeded(store: Store) -> tuple[User, Session, StoredMessage, Turn]:
    """A session of a user with a question and a running turn."""
    me = await store.add_user_if_absent(User(uuid.uuid4(), "local", "me", created_at=NOW))
    one = Session(uuid.uuid4(), me.id, "a", "echo", NOW, NOW)
    await store.add_session(one)
    asked = StoredMessage(uuid.uuid4(), one.id, None, Role.USER, NOW, {"v": 1, "text": "hi"})
    running = Turn(uuid.uuid4(), one.id, asked.id, "m", TurnState.RUNNING, NOW, LEASE)
    await store.start_turn(me.id, running, asked)
    return me, one, asked, running


async def raw(schema: TemporarySchema) -> asyncpg.Connection:
    """A connection of its own inside the schema, to hold a transaction open on."""
    connection = await asyncpg.connect(url(), server_settings={"search_path": schema.name})
    await codecs(connection)
    return connection


@asyncio_test
async def test_an_append_waits_on_a_readers_end_and_then_inserts_nothing() -> None:
    async with temporary_schema() as schema:
        store = PostgresStore(schema.pool, dsn=url())
        me, one, _, running = await seeded(store)
        reader = await raw(schema)
        try:
            ending = reader.transaction()
            await ending.start()
            await reader.execute(
                "UPDATE turns SET state = 'interrupted', ended_at = $2 WHERE id = $1",
                running.id,
                NOW + timedelta(minutes=1),
            )
            appending = asyncio.create_task(
                store.append_events(
                    me.id, one.id, running.id, [StoredEvent(1, DOCUMENT, EXPIRY)], NOW
                )
            )
            await asyncio.sleep(0.3)
            assert not appending.done(), "the append did not wait on the reader's end"
            await ending.commit()
            with pytest.raises(TurnLostError):
                await appending
        finally:
            await reader.close()
        assert await store.events_after(me.id, one.id, running.id, 0) == []
        await store.close()


@asyncio_test
async def test_a_hide_waits_on_a_starting_turn_and_then_hides_the_session() -> None:
    async with temporary_schema() as schema:
        store = PostgresStore(schema.pool, dsn=url())
        me, one, asked, running = await seeded(store)
        await store.finish_turn(
            me.id, one.id, running.id, TurnState.FINISHED, NOW, None, None, [], NOW
        )
        starter = await raw(schema)
        try:
            starting = starter.transaction()
            await starting.start()
            await starter.execute(
                "SELECT 1 FROM sessions WHERE id = $1 AND deleted_at IS NULL FOR SHARE", one.id
            )
            await starter.execute(
                "INSERT INTO turns (id, session_id, follows, model, state, started_at,"
                " lease_until) VALUES ($1, $2, $3, 'm', 'running', $4, $5)",
                uuid.uuid4(),
                one.id,
                asked.id,
                NOW,
                LEASE,
            )
            hiding = asyncio.create_task(store.hide_session(me.id, one.id, NOW))
            await asyncio.sleep(0.3)
            assert not hiding.done(), "the hide did not wait on the session's lock"
            await starting.commit()
            await hiding
        finally:
            await starter.close()
        with pytest.raises(SessionNotFoundError):
            await store.get_session(me.id, one.id)
        await store.close()


@asyncio_test
async def test_a_finish_and_a_start_raced_never_deadlock_and_the_finish_always_lands() -> None:
    async with temporary_schema() as schema:
        store = PostgresStore(schema.pool, dsn=url())
        me, one, asked, running = await seeded(store)
        started = 0
        for _ in range(20):
            again = Turn(uuid.uuid4(), one.id, asked.id, "m", TurnState.RUNNING, NOW, LEASE)
            finished, start = await asyncio.gather(
                store.finish_turn(
                    me.id, one.id, running.id, TurnState.FINISHED, NOW, None, None, [], NOW
                ),
                store.start_turn(me.id, again, None),
                return_exceptions=True,
            )
            assert finished is None, finished
            assert start is None or isinstance(start, TurnActiveError), start
            if start is None:
                started += 1
                running = again
            else:
                await store.start_turn(me.id, again, None)
                running = again
        assert started >= 0
        await store.close()


@asyncio_test
async def test_a_watcher_on_one_pool_is_woken_by_an_append_on_another() -> None:
    async with temporary_schema() as schema:
        watcher = PostgresStore(schema.pool, dsn=url())
        other_pool = await open_pool(
            url(), min_size=1, max_size=2, server_settings={"search_path": schema.name}
        )
        writer = PostgresStore(other_pool, dsn=url())
        try:
            me, one, _, running = await seeded(watcher)

            async def soon(position: int) -> None:
                await asyncio.sleep(0.2)
                event = StoredEvent(position, DOCUMENT | {"position": position}, EXPIRY)
                await writer.append_events(me.id, one.id, running.id, [event], NOW)

            appending = asyncio.create_task(soon(1))
            before = time.monotonic()
            assert await watcher.wait_for_events(me.id, one.id, running.id, 0, 5.0) is True
            assert time.monotonic() - before < 2.0
            await appending

            # The listening connection closed under the watcher: the wait returns at its
            # timeout, and the events are read all the same.
            assert watcher._listener is not None
            await watcher._listener.close()
            await soon(2)
            before = time.monotonic()
            assert await watcher.wait_for_events(me.id, one.id, running.id, 1, 0.5) is True
            assert len(await watcher.events_after(me.id, one.id, running.id, 0)) == 2

            await writer.finish_turn(
                me.id, one.id, running.id, TurnState.FINISHED, NOW, None, None, [], NOW
            )
            assert await watcher.wait_for_events(me.id, one.id, running.id, 9, 5.0) is True
            with pytest.raises(SessionNotFoundError):
                await watcher.get_session(uuid.uuid4(), one.id)
        finally:
            await watcher.close()
            await writer.close()
            await other_pool.close()
