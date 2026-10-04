# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The work connection: one connection per process, outside the pool, for the heartbeat.

A busy pool never delays a lease: the heartbeat waits on no request. Statements on it run one
at a time. It opens on first use, on the same database and search path as the pool, and a
statement that finds it broken drops it, so that the next opens it again.
"""

from __future__ import annotations

import asyncio
from typing import Any

import asyncpg

from robinauts.controller.adapters.postgres.pool import codecs

BROKEN: tuple[type[BaseException], ...] = (
    asyncpg.InterfaceError,
    asyncpg.ConnectionDoesNotExistError,
    OSError,
)


class WorkConnection:
    def __init__(self, dsn: str | None, pool: asyncpg.Pool) -> None:
        self._dsn = dsn
        self._pool = pool
        self._connection: asyncpg.Connection | None = None
        self._path: str | None = None
        self._lock = asyncio.Lock()

    @property
    def open(self) -> bool:
        return self._connection is not None and not self._connection.is_closed()

    async def fetch(self, query: str, *args: Any) -> list[asyncpg.Record]:
        async with self._lock:
            connection = await self._connected()
            try:
                return await connection.fetch(query, *args)
            except BROKEN:
                await self._drop()
                raise

    async def connect(self) -> None:
        """Open it now, rather than at its first statement."""
        async with self._lock:
            await self._connected()

    async def close(self) -> None:
        async with self._lock:
            await self._drop()

    async def _connected(self) -> asyncpg.Connection:
        if self._connection is not None and not self._connection.is_closed():
            return self._connection
        if self._dsn is None:
            raise RuntimeError("a store that renews leases needs the database's dsn")
        if self._path is None:
            # The pool's search path, whatever set it: the dsn, or the pool's own settings.
            # Read once, so that opening again never waits on the pool.
            self._path = await self._pool.fetchval("SELECT current_setting('search_path')")
        connection = await asyncpg.connect(self._dsn, server_settings={"search_path": self._path})
        await codecs(connection)
        self._connection = connection
        return connection

    async def _drop(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None and not connection.is_closed():
            connection.terminate()
