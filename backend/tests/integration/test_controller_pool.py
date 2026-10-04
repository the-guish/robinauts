# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The pool's codecs: a document written to a ``jsonb`` or a ``json`` column comes back equal.
And its bounds: a pool the configuration sizes, whose every acquire gives up in time."""

from __future__ import annotations

import time

import asyncpg
import pytest

from aio import asyncio_test
from controller_db import requires_postgres, temporary_schema, url
from robinauts.controller.adapters.postgres.pool import open_pool
from robinauts.controller.adapters.postgres.store import PostgresStore

pytestmark = requires_postgres

DOCUMENT = {
    "v": 1,
    "kind": "text_piece",
    "text": "18°C and clear in Lisbon \U0001f600",
    "nested": {"n": [1, 2.5, None, True], "s": "x"},
}


@asyncio_test
async def test_a_mapping_round_trips_through_both_json_types() -> None:
    async with temporary_schema(applied=False) as schema:
        await schema.pool.execute("CREATE TABLE docs (b jsonb, j json)")
        await schema.pool.execute("INSERT INTO docs VALUES ($1, $2)", DOCUMENT, DOCUMENT)
        row = await schema.pool.fetchrow("SELECT b, j FROM docs")
        assert row["b"] == DOCUMENT
        assert row["j"] == DOCUMENT


@asyncio_test
async def test_a_number_json_cannot_write_is_refused_before_the_database_sees_it() -> None:
    async with temporary_schema(applied=False) as schema:
        await schema.pool.execute("CREATE TABLE docs (b jsonb)")
        with pytest.raises(asyncpg.DataError, match="not JSON compliant"):
            await schema.pool.execute("INSERT INTO docs VALUES ($1)", {"x": float("nan")})


@asyncio_test
async def test_every_acquire_is_bounded_whoever_asks() -> None:
    pool = await open_pool(url(), max_size=2, acquire_timeout=0.2)
    try:
        async with pool.acquire(), pool.acquire():
            started = time.monotonic()
            with pytest.raises(TimeoutError):
                await pool.fetchval("SELECT 1")
            assert 0.15 < time.monotonic() - started < 2.0
        assert await pool.fetchval("SELECT 1") == 1
    finally:
        await pool.close()


@asyncio_test
async def test_the_store_opens_its_pool_as_the_configuration_says() -> None:
    async with temporary_schema() as schema:
        dsn = f"{url()}{'&' if '?' in url() else '?'}search_path={schema.name}"
        store = PostgresStore(dsn=dsn, pool_max=3, acquire_timeout=0.5)
        pool = await store.open()
        try:
            assert pool.get_max_size() == 3
            assert pool.acquire_timeout == 0.5
        finally:
            await store.close()
