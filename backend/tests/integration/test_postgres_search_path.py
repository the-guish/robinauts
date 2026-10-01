# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Which schema the checks judge, and which one the queries reach.

``schema.sql`` creates its tables unqualified, so they land in
``current_schema()`` -- the first schema on the connection's ``search_path``
that exists. Everything else has to agree with that, and there are two ways
not to.

Judging the *whole path* gets it wrong in both directions. A complete schema
further along makes an empty target look finished, so the command says
nothing needs doing and the first sign-in dies on a column that was never
created. Another application's ``users`` further along makes an empty target
look occupied, so the command refuses and names somebody else's tables.

Reaching a different relation than the one judged is the other way. The
stores run unqualified statements too; if anything the path finds first
answers to one of our names -- a temporary table is the easiest to make, and
the hardest to see -- the check passes and the writes go elsewhere.

Skipped, with the reason, when ``ROBINAUTS_TEST_DATABASE_URL`` is not set.
"""

from __future__ import annotations

from datetime import UTC, datetime

import asyncpg
import pytest

from aio import asyncio_test
from postgres import DATABASE_URL, requires_postgres, temporary_schema
from robinauts.legacy.datastore import (
    SCHEMA_TABLES,
    SCHEMA_VERSION,
    PostgresCredentialStore,
    check_schema,
    create_schema,
    schema_version,
)
from robinauts.legacy.domain import SchemaError

pytestmark = requires_postgres

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


async def _in_schema(name: str) -> asyncpg.Connection:
    """A connection of its own whose search path is that one schema."""
    assert DATABASE_URL is not None
    connection = await asyncpg.connect(DATABASE_URL)
    await connection.execute(f'SET search_path TO "{name}"')
    return connection


@asyncio_test
async def test_a_complete_schema_further_along_the_path_is_not_this_one() -> None:
    # The first way of getting it wrong. A whole schema in the second entry
    # of the path, a leftover table in the first: judge the path and the
    # database looks finished, judge the schema the file writes to and it is
    # the half-made thing it really is.
    async with temporary_schema(applied=False, behind=1) as schema:
        elsewhere = await _in_schema(schema.behind[0])
        try:
            await create_schema(elsewhere)
        finally:
            await elsewhere.close()
        await schema.pool.execute(f'CREATE TABLE "{schema.name}".users (id uuid PRIMARY KEY)')

        # What a check over the whole path would have seen: a version, and
        # every table, all of them in the wrong schema.
        assert await schema.pool.fetchval("SELECT to_regclass('schema_version')") is not None
        assert await schema.pool.fetchval("SELECT version FROM schema_version") == SCHEMA_VERSION

        # What the code sees.
        assert await schema_version(schema.pool) is None
        for refuse in (check_schema, create_schema):
            with pytest.raises(SchemaError) as refused:
                await refuse(schema.pool)
            assert "records no schema version" in str(refused.value)
            assert "users" in str(refused.value)


@asyncio_test
async def test_another_application_further_along_the_path_is_none_of_ours() -> None:
    # The other way of getting it wrong, and the more embarrassing one: an
    # empty database refused, with a stranger's table names in the message.
    async with temporary_schema(applied=False, behind=1) as schema:
        theirs = schema.behind[0]
        await schema.pool.execute(
            f'CREATE TABLE "{theirs}".users (login text PRIMARY KEY);'
            f' CREATE TABLE "{theirs}".sessions (token text PRIMARY KEY)'
        )

        await create_schema(schema.pool)

        await check_schema(schema.pool)
        assert await schema_version(schema.pool) == SCHEMA_VERSION
        # And ours were made in our schema, not theirs.
        assert await schema.pool.fetchval(
            "SELECT count(*) FROM pg_tables WHERE schemaname = $1", schema.name
        ) == len(SCHEMA_TABLES)
        # Theirs were left exactly as they were.
        assert await schema.pool.fetchval(
            "SELECT count(*) FROM information_schema.columns"
            " WHERE table_schema = $1 AND table_name = 'users' AND column_name = 'login'",
            theirs,
        )


@asyncio_test
async def test_the_store_really_works_in_that_arrangement() -> None:
    # The check passing is only worth something if the statements that follow
    # go to the same place.
    async with temporary_schema(behind=1) as schema:
        theirs = schema.behind[0]
        await schema.pool.execute(f'CREATE TABLE "{theirs}".users (login text PRIMARY KEY)')
        store = PostgresCredentialStore(schema.pool)

        user = await store.user_at_sign_in("google", "1", name="Ada", email=None, now=NOW)

        assert await store.user_by_id(user.id) == user
        assert await schema.pool.fetchval(
            "SELECT count(*) FROM pg_tables WHERE schemaname = $1 AND tablename = 'users'",
            schema.name,
        )
        assert await schema.pool.fetchval(f'SELECT count(*) FROM "{theirs}".users') == 0


@asyncio_test
async def test_a_table_the_path_finds_first_is_refused_rather_than_written_to() -> None:
    # `pg_temp` is searched before the rest of the path without ever being
    # named on it, which makes a temporary table the quietest way for the
    # schema that was checked and the schema that is written to to part
    # company. Any other relation reached first would do the same.
    async with temporary_schema(applied=False) as schema:
        connection = await _in_schema(schema.name)
        try:
            await create_schema(connection)
            await check_schema(connection)  # fine, so far

            await connection.execute("CREATE TEMP TABLE users (id uuid PRIMARY KEY)")

            with pytest.raises(SchemaError) as refused:
                await check_schema(connection)
            assert "search path reaches other tables" in str(refused.value)
            assert "users" in str(refused.value)
            # Not a database to make again: the path is what is wrong.
            assert "robinauts db init" not in str(refused.value)
            assert "search path" in refused.value.advice
        finally:
            await connection.close()


@asyncio_test
async def test_a_table_in_the_way_is_refused_before_the_file_is_applied() -> None:
    # The same question, asked at the other end. Creating our tables under
    # something that already answers to their names would leave a schema
    # that passes no check and takes no writes, so it is refused before a
    # single statement of the file runs.
    async with temporary_schema(applied=False) as schema:
        connection = await _in_schema(schema.name)
        try:
            await connection.execute("CREATE TEMP TABLE users (id uuid PRIMARY KEY)")

            with pytest.raises(SchemaError) as refused:
                await create_schema(connection)

            assert "search path reaches other tables" in str(refused.value)
            assert "users" in str(refused.value)
            # Nothing was written: not even the tables that are not in the way.
            assert await connection.fetchval(
                "SELECT count(*) FROM pg_tables WHERE schemaname = $1", schema.name
            ) == (0)
        finally:
            await connection.close()


@asyncio_test
async def test_a_search_path_that_names_nothing_is_said_so_plainly() -> None:
    # No `current_schema()` at all: there is nothing to read a version from
    # and nowhere to create a table, and PostgreSQL would only say so several
    # statements later, about a table, when the trouble is the connection.
    # `robinauts db init` cannot help, so it is not what the message offers.
    assert DATABASE_URL is not None
    connection = await asyncpg.connect(DATABASE_URL)
    try:
        await connection.execute('SET search_path TO "robinauts_no_such_schema"')

        for refuse in (check_schema, create_schema, schema_version):
            with pytest.raises(SchemaError) as refused:
                await refuse(connection)
            assert "names no schema that exists" in str(refused.value)
            assert "robinauts_no_such_schema" in str(refused.value)
            assert "robinauts db init" not in str(refused.value)
    finally:
        await connection.close()


@asyncio_test
async def test_a_view_by_the_name_of_a_table_is_not_that_table() -> None:
    # Finding a name is not finding a table. A view -- or a sequence, or a
    # foreign table -- of the right name would satisfy a check that only
    # asked whether something answered to it, and would then fail every
    # insert.
    async with temporary_schema() as schema:
        await schema.pool.execute(
            "DROP TABLE users CASCADE;"
            " CREATE TABLE real_users (id uuid PRIMARY KEY);"
            " CREATE VIEW users AS SELECT * FROM real_users"
        )

        assert await schema.pool.fetchval("SELECT to_regclass('users')") is not None
        for refuse in (check_schema, create_schema):
            with pytest.raises(SchemaError) as refused:
                await refuse(schema.pool)
            assert "missing: users" in str(refused.value)
