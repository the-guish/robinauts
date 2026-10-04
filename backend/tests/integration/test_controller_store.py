# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The PostgreSQL store keeps the store's contract, and the races the plan names."""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from aio import asyncio_test
from contracts.store import StoreContract
from controller_db import TemporarySchema, requires_postgres, temporary_schema, url
from robinauts.controller.adapters.postgres.credentials import PostgresCredentials
from robinauts.controller.adapters.postgres.pool import codecs, open_pool
from robinauts.controller.adapters.postgres.schema import create_schema
from robinauts.controller.adapters.postgres.store import PostgresStore
from robinauts.controller.contract.domain import (
    ApiToken,
    PendingLogin,
    Role,
    Session,
    SessionNotFoundError,
    Turn,
    TurnActiveError,
    TurnLostError,
    TurnState,
    User,
    UserSession,
)
from robinauts.controller.ports.store import Store, StoredEvent, StoredMessage
from robinauts.controller.ports.work import Held

pytestmark = requires_postgres

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
LEASE = NOW + timedelta(minutes=3)
EXPIRY = NOW + timedelta(hours=24)
DOCUMENT = {"v": 1, "kind": "text_piece", "position": 1, "text": "x"}
WORKER = "pod-a"


class TestPostgresStore(StoreContract):
    async def new_store(self) -> Store:
        schema = TemporarySchema()
        pool = await schema.open()
        await create_schema(pool)
        store = PostgresStore(pool, dsn=url())
        await store.open()
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
    running = Turn(
        uuid.uuid4(),
        one.id,
        asked.id,
        "m",
        TurnState.RUNNING,
        NOW,
        LEASE,
        worker_id=WORKER,
        attempt=1,
    )
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
                store.append_event(me.id, one.id, running.id, 1, DOCUMENT, NOW, EXPIRY)
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
                await writer.append_event(
                    me.id,
                    one.id,
                    running.id,
                    position,
                    DOCUMENT | {"position": position},
                    NOW,
                    EXPIRY,
                )

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


@asyncio_test
async def test_the_heartbeat_has_a_connection_of_its_own_and_a_busy_pool_does_not_delay_it() -> (
    None
):
    async with temporary_schema(size=2) as schema:
        store = PostgresStore(schema.pool, dsn=url())
        await store.open()
        _, _, _, running = await seeded(store)
        held = [Held(running.id, running.attempt)]
        try:
            # Every connection of the pool is taken, as by requests that hold on to them.
            async with schema.pool.acquire(), schema.pool.acquire():
                started = time.monotonic()
                beat = await asyncio.wait_for(
                    store.heartbeat(WORKER, held, NOW, timedelta(seconds=90)), 5.0
                )
                assert time.monotonic() - started < 5.0
            assert beat.lost == frozenset()
            # A dropped work connection is opened again by the next heartbeat.
            assert store._work is not None
            await store._work.close()
            again = await store.heartbeat(WORKER, held, NOW, timedelta(seconds=90))
            assert again.lost == frozenset()
        finally:
            await store.close()


@asyncio_test
async def test_two_readers_ending_one_expired_turn_keep_its_answer_once() -> None:
    async with temporary_schema() as schema:
        store = PostgresStore(schema.pool, dsn=url())
        me, one, asked, running = await seeded(store)
        left = StoredMessage(
            uuid.uuid4(), one.id, asked.id, Role.ASSISTANT, LEASE, {"v": 1, "text": "partial"}
        )
        later = LEASE + timedelta(minutes=1)
        ended = await asyncio.gather(
            *(
                store.end_expired_turn(me.id, one.id, later, turn=running.id, answer=left)
                for _ in range(4)
            )
        )
        assert len([e for e in ended if e is not None]) == 1
        assert await store.messages_of(me.id, one.id) == [{"v": 1, "text": "hi"}, left.document]


@asyncio_test
async def test_a_batch_of_events_is_announced_once() -> None:
    async with temporary_schema() as schema:
        store = PostgresStore(schema.pool, dsn=url())
        me, one, _, running = await seeded(store)
        heard: list[str] = []
        listener = await asyncpg.connect(url())
        try:
            await listener.add_listener("robinauts_turns", lambda *a: heard.append(a[-1]))
            batch = [
                StoredEvent(n, {"v": 1, "kind": "text_piece", "n": n}, EXPIRY) for n in (1, 2, 3)
            ]
            await store.append_events(me.id, one.id, running.id, batch, NOW)
            async with asyncio.timeout(5.0):
                while not heard:
                    await asyncio.sleep(0.01)
            await asyncio.sleep(0.1)
            assert heard == [f"{running.id} 3"]
        finally:
            await listener.close()
            await store.close()


@asyncio_test
async def test_readiness_names_a_listener_that_dropped_and_is_whole_again_after() -> None:
    async with temporary_schema() as schema:
        store = PostgresStore(schema.pool, dsn=url())
        await store.open()
        try:
            assert await store.problems() == []
            assert store._listener is not None
            await store._listener.close()
            assert await store.problems() == ["the listener is not connected"]
            # The next wait for events opens it again.
            await store._listen()
            assert await store.problems() == []
        finally:
            await store.close()


@asyncio_test
async def test_a_sweep_deletes_the_sign_in_records_that_expired() -> None:
    async with temporary_schema() as schema:
        store = PostgresStore(schema.pool, dsn=url())
        me, _, _, _ = await seeded(store)
        credentials = PostgresCredentials(store)
        hashes = [hashlib.sha256(str(n).encode()).hexdigest() for n in range(4)]
        await credentials.add_user_session(
            UserSession(uuid.uuid4(), me.id, hashes[0], NOW, NOW + timedelta(minutes=1))
        )
        await credentials.add_user_session(
            UserSession(uuid.uuid4(), me.id, hashes[1], NOW, NOW + timedelta(days=1))
        )
        await credentials.add_api_token(
            ApiToken(uuid.uuid4(), me.id, "t", hashes[2], NOW, NOW + timedelta(minutes=1))
        )
        await credentials.add_pending_login(
            PendingLogin(hashes[3], "p", "n", "v", "/", NOW, NOW + timedelta(minutes=1)), NOW
        )
        swept = await store.sweep_expired(NOW + timedelta(hours=1), 100)
        assert swept == {
            "turn_events": 0,
            "user_sessions": 1,
            "pending_logins": 1,
            "api_tokens": 1,
        }
        later = NOW + timedelta(hours=1)
        assert await credentials.resolve_user_session(hashes[1], later) == me


@asyncio_test
async def test_a_sweep_another_process_holds_is_skipped_and_two_at_once_sweep_once() -> None:
    async with temporary_schema() as schema:
        store = PostgresStore(schema.pool, dsn=url())
        me, one, _, running = await seeded(store)
        expired = [
            StoredEvent(n, {"v": 1, "n": n}, NOW + timedelta(seconds=1)) for n in range(1, 41)
        ]
        await store.append_events(me.id, one.id, running.id, expired, NOW)
        holder = await raw(schema)
        try:
            async with holder.transaction():
                taken = await holder.fetchval(
                    "SELECT pg_try_advisory_xact_lock(hashtext(current_schema()),"
                    " hashtext('sweep.turn_events'))"
                )
                assert taken is True
                skipped = await store.sweep_expired(LEASE, 100)
                assert skipped["turn_events"] == 0
        finally:
            await holder.close()
        other = PostgresStore(schema.pool, dsn=url())
        totals = await asyncio.gather(
            *(s.sweep_expired(LEASE, 7) for s in (store, other, store, other, store, other))
        )
        assert sum(t["turn_events"] for t in totals) <= 40
        while (await store.sweep_expired(LEASE, 7))["turn_events"]:
            pass
        assert await store.events_after(me.id, one.id, running.id, 0) == []
