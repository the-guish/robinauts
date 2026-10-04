# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The pool's codecs: a document written to a ``jsonb`` or a ``json`` column comes back equal."""

from __future__ import annotations

import time

import asyncpg
import pytest

from aio import asyncio_test
from controller_db import requires_postgres, temporary_schema, url
from robinauts.controller.adapters.postgres.pool import open_pool

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
async def test_an_acquire_waits_no_longer_than_its_timeout() -> None:
    async with temporary_schema(applied=False) as schema:
        pool = await open_pool(
            url(),
            min_size=1,
            max_size=1,
            server_settings={"search_path": schema.name},
            acquire_timeout=0.2,
        )
        try:
            held = await pool.acquire()
            before = time.monotonic()
            with pytest.raises(TimeoutError):
                await pool.fetchval("SELECT 1")
            assert time.monotonic() - before < 2
            await pool.release(held)
            assert await pool.fetchval("SELECT 1") == 1
        finally:
            await pool.close()
