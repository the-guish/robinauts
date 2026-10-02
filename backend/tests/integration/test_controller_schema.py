# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Creating the controller's schema, and refusing every database that is not empty or ours."""

from __future__ import annotations

import asyncio

import asyncpg
import pytest

from aio import asyncio_test
from controller_db import requires_postgres, temporary_schema, url
from robinauts.controller.adapters.postgres.schema import (
    SCHEMA_SHA256,
    SCHEMA_TABLES,
    check_schema,
    create_schema,
    schema_sql,
)
from robinauts.controller.contract.domain import ConfigError

pytestmark = requires_postgres


async def version_rows(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    return await pool.fetch("SELECT version, schema_sha256 FROM schema_version")


@asyncio_test
async def test_an_empty_schema_gets_the_whole_file_and_a_second_run_changes_nothing() -> None:
    async with temporary_schema(applied=False) as schema:
        await create_schema(schema.pool)
        await create_schema(schema.pool)
        rows = await version_rows(schema.pool)
        assert [(r["version"], r["schema_sha256"]) for r in rows] == [(1, SCHEMA_SHA256)]
        tables = await schema.pool.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
        )
        names = sorted(t["tablename"] for t in tables)
        assert names == sorted(SCHEMA_TABLES)
        assert {"user_sessions", "pending_logins", "api_tokens"} <= set(names)
        await check_schema(schema.pool)


@asyncio_test
async def test_a_database_with_no_schema_names_the_command() -> None:
    async with temporary_schema(applied=False) as schema:
        with pytest.raises(ConfigError, match="no schema: run `robinauts db init`"):
            await check_schema(schema.pool)


@asyncio_test
async def test_a_database_made_from_another_edit_of_the_file_is_refused() -> None:
    async with temporary_schema() as schema:
        await schema.pool.execute("UPDATE schema_version SET schema_sha256 = repeat('0', 64)")
        with pytest.raises(ConfigError, match="another edit of schema.sql"):
            await check_schema(schema.pool)
        with pytest.raises(ConfigError, match="another edit of schema.sql"):
            await create_schema(schema.pool)


@asyncio_test
async def test_a_file_applied_by_hand_records_no_hash_and_is_refused() -> None:
    async with temporary_schema(applied=False) as schema:
        async with schema.pool.acquire() as connection, connection.transaction():
            await connection.execute(schema_sql())
        with pytest.raises(ConfigError, match="applied by hand, no hash recorded"):
            await check_schema(schema.pool)


@asyncio_test
async def test_a_schema_of_another_version_is_refused_rather_than_relabelled() -> None:
    async with temporary_schema() as schema:
        await schema.pool.execute("UPDATE schema_version SET version = 2")
        with pytest.raises(ConfigError, match="schema version 2, and this build needs 1"):
            await create_schema(schema.pool)


@asyncio_test
async def test_a_half_applied_file_is_refused_rather_than_finished() -> None:
    async with temporary_schema() as schema:
        await schema.pool.execute("DROP TABLE turn_events")
        with pytest.raises(ConfigError, match="missing tables \\(turn_events\\)"):
            await check_schema(schema.pool)
        with pytest.raises(ConfigError, match="missing tables"):
            await create_schema(schema.pool)


@asyncio_test
async def test_four_commands_at_once_make_one_schema() -> None:
    async with temporary_schema(applied=False) as schema:
        connections = [
            await asyncpg.connect(url(), server_settings={"search_path": schema.name})
            for _ in range(4)
        ]
        try:
            await asyncio.gather(*(create_schema(c) for c in connections))
        finally:
            for connection in connections:
                await connection.close()
        assert len(await version_rows(schema.pool)) == 1
        await check_schema(schema.pool)


@asyncio_test
async def test_a_table_of_ours_ahead_on_the_search_path_is_refused() -> None:
    # A temporary table is searched before every schema on the path without being named
    # on it: an unqualified statement would hit it, so the check refuses the connection.
    async with temporary_schema() as schema:
        async with schema.pool.acquire() as connection:
            await connection.execute("CREATE TEMPORARY TABLE users (id uuid PRIMARY KEY)")
            with pytest.raises(ConfigError, match="reaches users in a schema ahead"):
                await check_schema(connection)
            await connection.execute("DROP TABLE pg_temp.users")
            await check_schema(connection)
