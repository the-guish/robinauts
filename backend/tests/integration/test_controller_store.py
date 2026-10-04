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
from contracts.store import FENCE, WORKER, StoreContract
from controller_db import TemporarySchema, requires_postgres, temporary_schema, url
from robinauts.controller.adapters.postgres import store as store_module
from robinauts.controller.adapters.postgres.pool import codecs, open_pool
from robinauts.controller.adapters.postgres.schema import create_schema
from robinauts.controller.adapters.postgres.store import PostgresStore
from robinauts.controller.contract.domain import (
    BusyError,
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
from robinauts.controller.ports.work import Held

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
        store = PostgresStore(pool, dsn=schema.dsn)
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
    running = held_turn(one.id, asked.id)
    await store.start_turn(me.id, running, asked)
    return me, one, asked, running


def held_turn(session: uuid.UUID, follows: uuid.UUID) -> Turn:
    return Turn(
        uuid.uuid4(),
        session,
        follows,
        "m",
        TurnState.RUNNING,
        NOW,
        LEASE,
        worker_id=WORKER,
        attempt=FENCE.attempt,
    )


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
                    me.id, one.id, running.id, FENCE, [StoredEvent(1, DOCUMENT, EXPIRY)], NOW
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
async def test_a_hide_waits_on_a_starting_turn_and_is_then_refused() -> None:
    async with temporary_schema() as schema:
        store = PostgresStore(schema.pool, dsn=url())
        me, one, asked, running = await seeded(store)
        await store.finish_turn(
            me.id, one.id, running.id, FENCE, TurnState.FINISHED, NOW, None, None, [], NOW
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
            with pytest.raises(TurnActiveError):
                await hiding
        finally:
            await starter.close()
        assert await store.get_session(me.id, one.id) == one
        assert await store.active_turn(me.id, one.id) is not None
        await store.close()


@asyncio_test
async def test_a_finish_and_a_start_raced_never_deadlock_and_the_finish_always_lands() -> None:
    async with temporary_schema() as schema:
        store = PostgresStore(schema.pool, dsn=url())
        me, one, asked, running = await seeded(store)
        started = 0
        for _ in range(20):
            again = held_turn(one.id, asked.id)
            finished, start = await asyncio.gather(
                store.finish_turn(
                    me.id, one.id, running.id, FENCE, TurnState.FINISHED, NOW, None, None, [], NOW
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
                await writer.append_events(me.id, one.id, running.id, FENCE, [event], NOW)

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
                me.id, one.id, running.id, FENCE, TurnState.FINISHED, NOW, None, None, [], NOW
            )
            assert await watcher.wait_for_events(me.id, one.id, running.id, 9, 5.0) is True
            with pytest.raises(SessionNotFoundError):
                await watcher.get_session(uuid.uuid4(), one.id)
        finally:
            await watcher.close()
            await writer.close()
            await other_pool.close()


@asyncio_test
async def test_the_heartbeat_has_a_connection_of_its_own_and_opens_it_again() -> None:
    async with temporary_schema(size=1) as schema:
        store = PostgresStore(schema.pool, dsn=schema.dsn)
        me, one, _, running = await seeded(store)
        held = [Held(running.id, FENCE.attempt)]
        try:
            # Every connection of the pool is taken, and the lease is renewed all the same.
            async with schema.pool.acquire():
                renewed = await asyncio.wait_for(
                    store.heartbeat(WORKER, held, NOW, timedelta(seconds=90)), 5.0
                )
            assert renewed.lost(held) == []
            assert store._work is not None
            store._work.terminate()
            renewed = await store.heartbeat(WORKER, held, NOW, timedelta(seconds=90))
            assert renewed.lost(held) == []
        finally:
            await store.close()


@asyncio_test
async def test_a_store_is_ready_when_its_database_and_connections_answer() -> None:
    async with temporary_schema() as schema:
        store = PostgresStore(schema.pool, dsn=schema.dsn)
        try:
            assert await store.readiness() == ()
        finally:
            await store.close()
        # A listening and a work connection that cannot be opened say so.
        nowhere = PostgresStore(schema.pool, dsn="postgresql://nobody@127.0.0.1:1/none")
        try:
            assert await nowhere.readiness() == (
                "the listening connection is down",
                "the work connection is down",
            )
        finally:
            await nowhere.close()


@asyncio_test
async def test_a_pool_with_no_connection_free_in_time_is_busy_not_stuck() -> None:
    async with temporary_schema(size=1) as schema:
        store = PostgresStore(schema.pool, dsn=schema.dsn, acquire_timeout=0.2)
        me, one, _, _ = await seeded(store)
        try:
            async with schema.pool.acquire():
                with pytest.raises(BusyError):
                    await asyncio.wait_for(store.get_session(me.id, one.id), 5.0)
            assert await store.get_session(me.id, one.id) == one
        finally:
            await store.close()


@asyncio_test
async def test_a_sweep_task_another_process_holds_is_left_to_it() -> None:
    async with temporary_schema() as schema:
        store = PostgresStore(schema.pool, dsn=schema.dsn)
        holder = await raw(schema)
        try:
            holding = holder.transaction()
            await holding.start()
            await holder.execute(
                "SELECT pg_advisory_xact_lock(hashtext('robinauts.sweep.expired'))"
            )
            assert await store.delete_expired(NOW, 10) is None
            assert await store.expired_turns(NOW, 10) == []
            await holding.rollback()
            assert await store.delete_expired(NOW, 10) == {
                "turn_events": 0,
                "user_sessions": 0,
                "api_tokens": 0,
                "pending_logins": 0,
            }
        finally:
            await holder.close()
            await store.close()


@asyncio_test
async def test_two_processes_sweeping_at_once_delete_everything_expired_once() -> None:
    async with temporary_schema(size=8) as schema:
        first = PostgresStore(schema.pool, dsn=schema.dsn)
        second = PostgresStore(schema.pool, dsn=schema.dsn)
        me, one, _, running = await seeded(first)
        expired = [
            StoredEvent(n, DOCUMENT | {"position": n}, NOW - timedelta(minutes=1))
            for n in range(1, 251)
        ]
        await first.append_events(me.id, one.id, running.id, FENCE, expired, NOW)
        async with schema.pool.acquire() as connection:
            for n in range(30):
                await connection.execute(
                    "INSERT INTO user_sessions (id, user_id, secret_hash, created_at, expires_at)"
                    " VALUES ($1, $2, $3, $4, $5)",
                    uuid.uuid4(),
                    me.id,
                    f"{n:064x}",
                    NOW - timedelta(days=2),
                    NOW - timedelta(days=1),
                )
        try:
            results = await asyncio.gather(
                *(store.delete_expired(NOW, 7) for store in (first, second, first, second))
            )
            done = [r for r in results if r is not None]
            assert done
            assert sum(r["turn_events"] for r in done) == 250
            assert sum(r["user_sessions"] for r in done) == 30
            async with schema.pool.acquire() as connection:
                assert await connection.fetchval("SELECT count(*) FROM turn_events") == 0
                assert await connection.fetchval("SELECT count(*) FROM user_sessions") == 0
        finally:
            await first.close()
            await second.close()


@asyncio_test
async def test_a_work_connection_that_hangs_is_dropped_and_opened_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(store_module, "WORK_TIMEOUT", 0.3)
    async with temporary_schema() as schema:
        store = PostgresStore(schema.pool, dsn=schema.dsn)
        me, one, _, running = await seeded(store)
        held = [Held(running.id, FENCE.attempt)]
        try:
            with pytest.raises(TimeoutError):
                await store._on_work_connection(lambda c: c.fetchval("SELECT pg_sleep(5)"))
            assert store._work is None
            renewed = await store.heartbeat(WORKER, held, NOW, timedelta(seconds=90))
            assert renewed.lost(held) == []
        finally:
            await store.close()


@asyncio_test
async def test_closing_a_store_whose_database_hangs_takes_one_bounded_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(store_module, "CLOSE_SECONDS", 0.3)
    store = PostgresStore(dsn=url())

    class Hanging:
        terminated = False

        async def close(self) -> None:
            await asyncio.sleep(3600)

        def terminate(self) -> None:
            self.terminated = True

    work, listener = Hanging(), Hanging()
    store._work, store._listener = work, listener  # type: ignore[assignment]
    before = time.monotonic()
    await store.close()
    assert time.monotonic() - before < 1.0
    assert work.terminated
    assert listener.terminated
