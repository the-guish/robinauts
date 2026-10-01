# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""``PostgresCredentialStore`` against a real PostgreSQL.

The whole of ``tests/contracts/credential_store.py``, the suite the in-memory
fake passes, run against the database -- which is the point of having written
the suite once. What the store must do is said in the port and checked in the
contract, and a promise tested only in this file would be a promise the fake
is free to break.

What is here is what only a database can be asked: that the pool really hands
out several connections, so the contract's concurrency tests are not passing
one statement at a time; and that a time survives the round trip as the
instant it was, whatever zone the server and the session are in.

Skipped, with the reason, when ``ROBINAUTS_TEST_DATABASE_URL`` is not set.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, timezone

import asyncpg
import pytest

from aio import asyncio_test
from contracts.credential_store import CredentialStoreContract, pending
from postgres import (
    TemporarySchema,
    database_required,
    requires_postgres,
    schema_exists,
    temporary_schema,
)
from robinauts.legacy.core import secret_hash
from robinauts.legacy.datastore import PostgresCredentialStore, create_schema
from robinauts.legacy.domain import DatabaseUnreachableError
from robinauts.legacy.ports import CredentialStore

pytestmark = requires_postgres

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
MUCH_LATER = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)

KATHMANDU = timezone(timedelta(hours=5, minutes=45))
"""An offset with a quarter hour in it: a zone that catches a wrong guess."""

CHATHAM = "Pacific/Chatham"
"""A session zone at +12:45 or +13:45, and not the one the server is on."""


class TestPostgresCredentialStore(CredentialStoreContract):
    """The store contract, on PostgreSQL, in a schema of this test's own."""

    def setup_method(self) -> None:
        self.schema = TemporarySchema()

    async def new_store(self) -> CredentialStore:
        pool = await self.schema.open()
        try:
            await create_schema(pool)
        except BaseException:
            # Between the pool and the schema there is a moment where a
            # failure would leave both behind: a schema on everybody's
            # database and eight connections on the server, for as long as
            # the process lives.
            await self.schema.close()
            raise
        return PostgresCredentialStore(pool)

    async def close_store(self, store: CredentialStore | None) -> None:
        await self.schema.close()

    async def dump(self, store: CredentialStore) -> str:
        return await self.schema.dump()

    @asyncio_test
    async def test_the_concurrency_tests_above_ran_against_a_real_pool(self) -> None:
        # A store on one connection would pass every `asyncio.gather` test in
        # the contract by doing the calls one after another. This is what says
        # they did not: the pool takes more than one connection, and four
        # statements started together really are on more than one backend.
        async with self.opened() as store:
            assert isinstance(store, PostgresCredentialStore)
            assert self.schema.pool.get_max_size() > 1

            backends = await asyncio.gather(
                *(self.schema.pool.fetchval("SELECT pg_backend_pid()") for _ in range(4))
            )

            assert len(set(backends)) > 1


@asyncio_test
async def test_a_time_comes_back_as_the_instant_it_went_in() -> None:
    # The server this runs against is deliberately not on UTC, and the session
    # below is on a zone odder still. Neither may change what a stored time
    # means: `timestamptz` keeps an instant, and an expiry read as a wall clock
    # would lengthen or shorten every session by the offset of whoever asked.
    async with temporary_schema(server_settings={"timezone": CHATHAM}) as schema:
        store = PostgresCredentialStore(schema.pool)
        assert await schema.pool.fetchval("SELECT current_setting('TimeZone')") == CHATHAM

        # One instant, written with an offset; one written in UTC.
        created_at = datetime(2026, 9, 21, 17, 45, tzinfo=KATHMANDU)
        expires_at = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
        user = await store.user_at_sign_in("google", "1", name=None, email=None, now=created_at)
        session = await store.add_session(
            secret_hash("cookie"), user.id, created_at=created_at, expires_at=expires_at
        )
        await store.add_pending_login(
            secret_hash("state"),
            pending(created_at=created_at, expires_at=expires_at),
            limit=10,
            now=created_at,
        )

        found = await store.session_by_hash(secret_hash("cookie"), now=created_at)
        login = await store.take_pending_login(secret_hash("state"))

        assert found is not None
        assert login is not None
        for when in (user.created_at, session.created_at, found.created_at, login.created_at):
            assert when is not None
            assert when.tzinfo is not None
            assert when == created_at
            assert when == datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
        for deadline in (session.expires_at, found.expires_at, login.expires_at):
            assert deadline.tzinfo is not None
            assert deadline == expires_at

        # And the decision made from those times is the one the application
        # would make with its own clock, not one the session's zone shifted.
        assert await store.session_by_hash(secret_hash("cookie"), now=expires_at) is None


@asyncio_test
async def test_a_constraint_this_code_never_heard_of_is_not_an_answer() -> None:
    # The store turns two constraint violations into answers for its caller,
    # and tells them apart by the constraint's **name**. By class it could
    # not: the day somebody adds a second unique index to `sessions`, every
    # violation of it would be reported as "a session is already stored under
    # that hash" -- about a row whose hash was perfectly fine. A constraint
    # nobody planned to violate is a bug, and arrives as one.
    async with temporary_schema() as schema:
        await schema.pool.execute("CREATE UNIQUE INDEX one_each ON sessions (user_id)")
        store = PostgresCredentialStore(schema.pool)
        user = await store.user_at_sign_in("google", "1", name=None, email=None, now=NOW)
        await store.add_session(
            secret_hash("first"), user.id, created_at=NOW, expires_at=MUCH_LATER
        )

        with pytest.raises(asyncpg.exceptions.UniqueViolationError) as raw:
            await store.add_session(
                secret_hash("second"), user.id, created_at=NOW, expires_at=MUCH_LATER
            )

        assert raw.value.constraint_name == "one_each"


@asyncio_test
async def test_the_required_database_is_not_on_utc() -> None:
    # CI runs its PostgreSQL in a quarter-hour zone on purpose, so that a
    # store reading a time as a wall clock fails there. Nothing was checking
    # that the service still does it, and a setting nobody asserts is a
    # setting that quietly disappears in an edit.
    if not database_required():
        pytest.skip("only a run that requires a database promises an odd time zone")
    async with temporary_schema(applied=False) as schema:
        zone = await schema.pool.fetchval("SHOW timezone")

        assert zone != "UTC", (
            "the PostgreSQL these tests are required to run against is meant to be in an odd"
            " time zone, so that a wall-clock bug shows up here rather than in a deployment"
        )


@asyncio_test
async def test_a_schema_that_cannot_be_opened_is_dropped_again() -> None:
    # Eight connections and a schema are taken in two steps, and the first
    # can succeed while the second fails -- a setting the server refuses, a
    # server that has run out of connections. What must not happen is that
    # the schema stays behind: one test's bad argument would then leave a
    # schema in everybody's database and the mess would outlive the run.
    schema = TemporarySchema(server_settings={"timezone": "Not/AZone"})

    # `DatabaseUnreachableError` and not the driver's own class: `open_pool`
    # turns every way a pool refuses to open into one of ours, so that a
    # command prints it as a line rather than a traceback about asyncpg
    # (tests/unit/test_database_errors.py). This is that happening against a
    # real server, for a setting it really refuses.
    with pytest.raises(DatabaseUnreachableError):
        await schema.open()

    assert await schema_exists(schema.name) is False


@asyncio_test
async def test_a_store_that_fails_half_way_through_being_built_is_still_closed() -> None:
    # The contract calls `new_store` inside its `try`, so a store that got as
    # far as taking something and then failed is handed to `close_store` all
    # the same. Without that, every failing test would leak a schema and a
    # pool, and the run would go on to fail somewhere else entirely.
    made: list[TemporarySchema] = []

    class FailsAfterTakingTheSchema(TestPostgresCredentialStore):
        async def new_store(self) -> CredentialStore:
            await self.schema.open()
            made.append(self.schema)
            raise RuntimeError("the store could not be built")

    broken = FailsAfterTakingTheSchema()
    broken.setup_method()

    with pytest.raises(RuntimeError, match="could not be built"):
        async with broken.opened():
            pass  # pragma: no cover - never reached

    assert len(made) == 1
    assert await schema_exists(made[0].name) is False


@asyncio_test
async def test_a_naive_time_is_refused_rather_than_read_as_utc() -> None:
    # asyncpg would take a naive datetime for a `timestamptz` and call it UTC.
    # A deployment five and three quarter hours from UTC would then find every
    # session that much longer or shorter, and nothing would have said so.
    async with temporary_schema() as schema:
        store = PostgresCredentialStore(schema.pool)
        naive = datetime(2026, 9, 21, 12, 0)

        with pytest.raises(ValueError, match="aware datetime"):
            await store.count_pending_logins(now=naive)
        with pytest.raises(ValueError, match="aware datetime"):
            await store.delete_expired_sessions(now=naive)
        with pytest.raises(ValueError, match="aware datetime"):
            await store.user_at_sign_in("google", "1", name=None, email=None, now=naive)
