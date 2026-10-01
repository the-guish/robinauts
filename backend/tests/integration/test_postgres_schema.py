# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Creating the schema, and refusing every database that is not empty or ours.

The server never creates the schema; a command does, and the server checks
what it finds before it agrees to serve anything (``docs/specs/backend.md``).
Both halves are here, and the half that matters most is what the command
does **not** do: there are no migrations yet, so a database that already
holds a schema is one to be made again, not one to apply the file over.
Applying it looks idempotent and is not -- `CREATE TABLE IF NOT EXISTS`
leaves an older table exactly as it is -- so the test below builds a
version-0 database by hand and requires the command to refuse it.

The few constraints the stores rely on but never exercise are here too: the
cascade from a user to their sessions, and the columns that refuse anything
but a hash.

Skipped, with the reason, when ``ROBINAUTS_TEST_DATABASE_URL`` is not set.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import asyncpg
import pytest

from aio import asyncio_test
from postgres import DATABASE_URL, requires_postgres, temporary_schema
from robinauts.legacy.datastore import (
    SCHEMA_SHA256,
    SCHEMA_TABLES,
    SCHEMA_VERSION,
    check_schema,
    create_schema,
    schema_sql,
    schema_version,
)
from robinauts.legacy.domain import SchemaError

pytestmark = requires_postgres

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)

_NEW_USER = (
    "INSERT INTO users (provider, subject, created_at) VALUES ('google', $1, $2) RETURNING id"
)
_NEW_SESSION = (
    "INSERT INTO sessions (secret_hash, user_id, created_at, expires_at) VALUES ($1, $2, $3, $3)"
)


# Creating it.


@asyncio_test
async def test_an_empty_database_gets_the_whole_schema() -> None:
    async with temporary_schema(applied=False) as schema:
        await create_schema(schema.pool)

        assert await schema_version(schema.pool) == SCHEMA_VERSION
        await check_schema(schema.pool)


@asyncio_test
async def test_creating_it_again_changes_nothing() -> None:
    # `robinauts db init` on a database that already has exactly this schema
    # is a no-op, not an error: an operator who is not sure may simply run it.
    async with temporary_schema() as schema:
        user = await schema.pool.fetchval(_NEW_USER, "1", NOW)
        applied = await schema.pool.fetchval("SELECT applied_at FROM schema_version")

        await create_schema(schema.pool)

        assert await schema_version(schema.pool) == SCHEMA_VERSION
        assert await schema.pool.fetchval("SELECT id FROM users") == user
        assert await schema.pool.fetchval("SELECT count(*) FROM schema_version") == 1
        # Not even the date it was created: the second run wrote nothing.
        assert await schema.pool.fetchval("SELECT applied_at FROM schema_version") == applied


@asyncio_test
async def test_a_schema_of_another_version_is_refused_rather_than_relabelled() -> None:
    # The finding this test exists for. A version-0 `users` without `email`,
    # the file applied over it: `CREATE TABLE IF NOT EXISTS` would leave the
    # old table alone and the version row would call it this build's version,
    # after which every check passes and the first sign-in fails on a missing
    # column. So the command refuses, and says the database is made again.
    async with temporary_schema(applied=False) as schema:
        await schema.pool.execute("""
            CREATE TABLE schema_version (
                only_row boolean PRIMARY KEY DEFAULT true CHECK (only_row),
                version integer NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now()
            );
            INSERT INTO schema_version (version) VALUES (0);
            CREATE TABLE users (
                id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                provider text NOT NULL,
                subject text NOT NULL,
                created_at timestamptz NOT NULL,
                UNIQUE (provider, subject)
            );
            """)

        with pytest.raises(SchemaError) as refused:
            await create_schema(schema.pool)

        assert refused.value.found == 0
        assert "schema version 0" in str(refused.value)
        assert "empty database only" in str(refused.value)
        # And the database was left exactly as it was found.
        assert await schema_version(schema.pool) == 0
        columns = await schema.pool.fetchval(
            "SELECT count(*) FROM information_schema.columns"
            " WHERE table_schema = current_schema() AND table_name = 'users'"
            " AND column_name = 'email'"
        )
        assert columns == 0


@asyncio_test
async def test_our_tables_without_a_version_are_refused() -> None:
    # A database somebody made by hand, or one an interrupted `psql` left
    # half way through. There is no telling what shape those tables are in,
    # so it is not one to add the rest of a schema to.
    async with temporary_schema(applied=False) as schema:
        await schema.pool.execute("CREATE TABLE users (id uuid PRIMARY KEY)")

        with pytest.raises(SchemaError) as refused:
            await create_schema(schema.pool)

        assert refused.value.found is None
        assert "records no schema version" in str(refused.value)
        assert "users" in str(refused.value)


@asyncio_test
async def test_a_version_table_of_another_shape_is_an_unknown_version() -> None:
    # Somebody else's `schema_version`, or a shape of ours older than any
    # this build knows. Calling that "no schema" would invite the operator
    # to create one on top of it.
    async with temporary_schema(applied=False) as schema:
        await schema.pool.execute("CREATE TABLE schema_version (revision text PRIMARY KEY)")

        with pytest.raises(SchemaError) as unreadable:
            await schema_version(schema.pool)
        assert "unknown version" in str(unreadable.value)
        with pytest.raises(SchemaError):
            await create_schema(schema.pool)
        with pytest.raises(SchemaError):
            await check_schema(schema.pool)


@asyncio_test
async def test_a_half_applied_file_is_refused_rather_than_finished() -> None:
    # What `psql -f` without `--single-transaction` leaves: the statements
    # that ran, and no version row, because the version is the last statement
    # of the file. Both the command and the check say no.
    async with temporary_schema(applied=False) as schema:
        await schema.pool.execute(
            "".join(
                block
                for block in _statements(schema_sql())
                if "pending_logins" not in block and "INSERT INTO" not in block
            )
        )

        assert await schema_version(schema.pool) is None
        with pytest.raises(SchemaError) as refused:
            await check_schema(schema.pool)
        assert "records no schema version" in str(refused.value)
        with pytest.raises(SchemaError):
            await create_schema(schema.pool)


@asyncio_test
async def test_a_created_schema_records_the_file_it_was_made_from() -> None:
    async with temporary_schema(applied=False) as schema:
        await create_schema(schema.pool)

        assert await schema.pool.fetchval("SELECT schema_sha256 FROM schema_version") == (
            SCHEMA_SHA256
        )


@asyncio_test
async def test_a_database_made_from_an_older_file_is_refused_and_told_why() -> None:
    # The finding this test exists for. Before the first release every edit
    # of schema.sql is version 1, so a database made from an older edit
    # passes the version check -- and, missing a column, would fail in the
    # middle of a request. The hash the command recorded is what tells it
    # apart, and it is checked before the tables so that the sentence is
    # "older schema.sql" and not "a table is missing".
    async with temporary_schema() as schema:
        await schema.pool.execute("UPDATE schema_version SET schema_sha256 = $1", "0" * 64)

        with pytest.raises(SchemaError) as refused:
            await check_schema(schema.pool)

        assert refused.value.found == SCHEMA_VERSION
        assert "made from an older schema.sql" in str(refused.value)
        assert "drop it and run" in str(refused.value)
        assert "robinauts db init" in str(refused.value)
        with pytest.raises(SchemaError):
            await create_schema(schema.pool)
        # And left as it was found: not relabelled as this build's.
        assert await schema.pool.fetchval("SELECT schema_sha256 FROM schema_version") == "0" * 64


@asyncio_test
async def test_a_file_applied_by_hand_records_no_hash_and_is_refused() -> None:
    # `psql -f schema.sql`, whole: the version row is there, the hash is not,
    # and there is no telling which edit of the file it was.
    async with temporary_schema(applied=False) as schema:
        await schema.pool.execute(schema_sql())
        assert await schema_version(schema.pool) == SCHEMA_VERSION

        with pytest.raises(SchemaError) as refused:
            await check_schema(schema.pool)

        assert "made from an older schema.sql" in str(refused.value)
        assert "not by `robinauts db init`" in str(refused.value)
        with pytest.raises(SchemaError):
            await create_schema(schema.pool)


@asyncio_test
async def test_a_version_row_from_before_the_hash_was_recorded_is_refused() -> None:
    # A database made from an edit of the file that had no `schema_sha256`
    # column at all: the same refusal, not an error about a column.
    async with temporary_schema() as schema:
        await schema.pool.execute("ALTER TABLE schema_version DROP COLUMN schema_sha256")

        with pytest.raises(SchemaError) as refused:
            await check_schema(schema.pool)

        assert "made from an older schema.sql" in str(refused.value)


@asyncio_test
async def test_four_commands_at_once_make_one_schema() -> None:
    # Look, then write, with nothing in between: two of these against one
    # empty database would both find it empty and both run the file, and
    # what comes back is a unique violation from PostgreSQL's own catalogue
    # -- an error about `pg_type` that means nothing to whoever ran the
    # command. An advisory lock on the schema makes the second one wait and
    # then find the first one's work.
    async with temporary_schema(applied=False) as schema:
        await asyncio.gather(*(create_schema(schema.pool) for _ in range(4)))

        await check_schema(schema.pool)
        assert await schema.pool.fetchval("SELECT count(*) FROM schema_version") == 1
        assert await schema.pool.fetchval(
            "SELECT count(*) FROM pg_tables WHERE schemaname = current_schema()"
        ) == len(SCHEMA_TABLES)


# Checking it.


@asyncio_test
async def test_the_check_passes_on_a_freshly_created_schema() -> None:
    async with temporary_schema() as schema:
        assert await schema_version(schema.pool) == SCHEMA_VERSION
        await check_schema(schema.pool)


@asyncio_test
async def test_the_schema_applies_over_a_single_connection_too() -> None:
    # The command that will apply it has one connection, not a pool.
    async with temporary_schema(applied=False) as schema:
        assert DATABASE_URL is not None
        connection = await asyncpg.connect(DATABASE_URL)
        try:
            await connection.execute(f'SET search_path TO "{schema.name}"')

            await create_schema(connection)

            assert await schema_version(connection) == SCHEMA_VERSION
            await check_schema(connection)
        finally:
            await connection.close()


@asyncio_test
async def test_a_database_with_no_schema_is_told_which_command_makes_one() -> None:
    async with temporary_schema(applied=False) as schema:
        assert await schema_version(schema.pool) is None

        with pytest.raises(SchemaError) as refused:
            await check_schema(schema.pool)

        assert refused.value.found is None
        assert refused.value.expected == SCHEMA_VERSION
        assert "no Robinauts schema" in str(refused.value)
        assert "robinauts db init" in str(refused.value)


@asyncio_test
async def test_a_recorded_version_that_is_not_ours_is_refused_and_both_named() -> None:
    async with temporary_schema() as schema:
        await schema.pool.execute("UPDATE schema_version SET version = $1", SCHEMA_VERSION + 1)

        with pytest.raises(SchemaError) as refused:
            await check_schema(schema.pool)

        assert refused.value.found == SCHEMA_VERSION + 1
        assert f"schema version {SCHEMA_VERSION + 1}" in str(refused.value)
        assert f"needs schema version {SCHEMA_VERSION}" in str(refused.value)
        assert "robinauts db init" in str(refused.value)


@asyncio_test
async def test_the_right_version_with_a_table_missing_is_refused_and_named() -> None:
    # A version row is a claim, not proof. Somebody dropped a table, or a
    # restore only got half way; either way the server must not start.
    async with temporary_schema() as schema:
        await schema.pool.execute("DROP TABLE pending_logins")

        with pytest.raises(SchemaError) as refused:
            await check_schema(schema.pool)

        assert refused.value.found == SCHEMA_VERSION
        assert "missing: pending_logins" in str(refused.value)
        with pytest.raises(SchemaError):
            await create_schema(schema.pool)


@asyncio_test
async def test_a_version_row_that_was_deleted_counts_as_no_version() -> None:
    async with temporary_schema() as schema:
        await schema.pool.execute("DELETE FROM schema_version")

        assert await schema_version(schema.pool) is None
        with pytest.raises(SchemaError) as refused:
            await check_schema(schema.pool)
        # Our tables are there, so it is not an empty database.
        assert "records no schema version" in str(refused.value)


@asyncio_test
async def test_the_check_looks_for_every_table_the_build_expects() -> None:
    # Whichever one goes missing, the check names it. A table added to the
    # file but not to SCHEMA_TABLES would never be looked for.
    for table in SCHEMA_TABLES:
        if table == "schema_version":
            continue  # dropping it is the "no version" case above
        async with temporary_schema() as schema:
            await schema.pool.execute(f"DROP TABLE {table} CASCADE")

            with pytest.raises(SchemaError) as refused:
                await check_schema(schema.pool)

            assert f"missing: {table}" in str(refused.value)


# The constraints the stores lean on.


@asyncio_test
async def test_the_hash_columns_refuse_anything_but_a_hash() -> None:
    # The last line of defence for the one promise of the port: what is
    # looked up by a secret is looked up by the SHA-256 of it. A caller that
    # passed the secret itself would store it -- and here, would not.
    async with temporary_schema() as schema:
        user = await schema.pool.fetchval(_NEW_USER, "1", NOW)

        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await schema.pool.execute(_NEW_SESSION, "a-secret-nobody-hashed", user, NOW)
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await schema.pool.execute(
                "INSERT INTO pending_logins"
                " (state_hash, provider, nonce, verifier, created_at, expires_at)"
                " VALUES ($1, 'google', 'n', 'v', $2, $2)",
                "A" * 64,  # upper case is not what secret_hash writes
                NOW,
            )


@asyncio_test
async def test_a_deleted_user_takes_their_sessions_with_them() -> None:
    # The foreign key cascades, which is what makes deleting a user one
    # statement rather than a list of tables to remember.
    async with temporary_schema() as schema:
        user = await schema.pool.fetchval(_NEW_USER, "1", NOW)
        other = await schema.pool.fetchval(_NEW_USER, "2", NOW)
        await schema.pool.execute(_NEW_SESSION, "a" * 64, user, NOW)
        await schema.pool.execute(_NEW_SESSION, "b" * 64, other, NOW)

        await schema.pool.execute("DELETE FROM users WHERE id = $1", user)

        assert await schema.pool.fetchval("SELECT count(*) FROM sessions") == 1
        assert await schema.pool.fetchval("SELECT user_id FROM sessions") == other


def _statements(sql: str) -> list[str]:
    """The file cut after each statement, comments and all.

    Crude on purpose: the point is to feed PostgreSQL part of the real file,
    the way an interrupted `psql` would, not to parse SQL.
    """
    return [block + ";" for block in sql.split(";") if block.strip()]
