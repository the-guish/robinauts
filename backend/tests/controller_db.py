# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A PostgreSQL for the controller's database tests, and a schema of their own in it.

The URL comes from ``ROBINAUTS_TEST_DATABASE_URL``, the tests skip with a reason when
there is none, and every test gets a schema named after a fresh uuid, dropped however it
ends.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg
import pytest

from robinauts.controller.adapters.postgres.pool import open_pool
from robinauts.controller.adapters.postgres.schema import create_schema

DATABASE_URL = os.environ.get("ROBINAUTS_TEST_DATABASE_URL")

NO_DATABASE = (
    "no database: set ROBINAUTS_TEST_DATABASE_URL to a PostgreSQL 14 or later"
    " that these tests may create and drop schemas in"
)

requires_postgres = [
    pytest.mark.io,
    pytest.mark.database,
    pytest.mark.skipif(DATABASE_URL is None, reason=NO_DATABASE),
]


def url() -> str:
    if DATABASE_URL is None:
        raise RuntimeError(NO_DATABASE)
    return DATABASE_URL


class TemporarySchema:
    """A schema of its own, and a pool whose every connection has ``search_path`` set to it.

    The pool is warm: ``min_size`` equals ``max_size``, so the races the store's tests run
    really overlap instead of waiting on connections still opening.
    """

    def __init__(self, *, size: int = 8, behind: int = 0) -> None:
        self.name = "robinauts_test_" + uuid.uuid4().hex
        self.behind = tuple(f"{self.name}_behind_{n}" for n in range(behind))
        self._size = size
        self._pool: asyncpg.Pool | None = None

    @property
    def dsn(self) -> str:
        """The test database, with the search path on this schema: what a connection of the
        store's own, outside the pool, opens."""
        joiner = "&" if "?" in url() else "?"
        return f"{url()}{joiner}search_path={self.name}"

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("this schema is not open")
        return self._pool

    async def open(self) -> asyncpg.Pool:
        for name in (self.name, *self.behind):
            await self._alone(f'CREATE SCHEMA "{name}"')
        try:
            self._pool = await open_pool(
                url(),
                min_size=self._size,
                max_size=self._size,
                server_settings={"search_path": ", ".join([self.name, *self.behind])},
            )
        except BaseException:
            await self.close()
            raise
        return self._pool

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
        for name in (self.name, *self.behind):
            await self._alone(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')

    async def _alone(self, statement: str) -> None:
        connection = await asyncpg.connect(url())
        try:
            await connection.execute(statement)
        finally:
            await connection.close()


@asynccontextmanager
async def temporary_schema(
    *, applied: bool = True, behind: int = 0, size: int = 8
) -> AsyncIterator[TemporarySchema]:
    """A schema of this test's own, with ``schema.sql`` applied unless asked otherwise."""
    schema = TemporarySchema(size=size, behind=behind)
    await schema.open()
    try:
        if applied:
            await create_schema(schema.pool)
        yield schema
    finally:
        await schema.close()
