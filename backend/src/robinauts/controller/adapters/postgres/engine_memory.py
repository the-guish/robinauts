# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The engines' memory on PostgreSQL: a pool of the worker's own, for the engines' tables.

The schema is not checked here: the server that reads the core's tables checks it, and the
engines' tables are the ones ``robinauts db init`` set up.
"""

from __future__ import annotations

import asyncio
import logging

import asyncpg

from robinauts.controller.adapters.postgres.pool import COMMAND_TIMEOUT, open_pool
from robinauts.controller.ports.engine_memory import EngineMemory

_log = logging.getLogger(__name__)


class PostgresEngineMemory(EngineMemory):
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    async def open(self) -> asyncpg.Pool:
        if self._pool is None:
            self._pool = await open_pool(self._dsn)
        return self._pool

    async def close(self) -> None:
        if self._pool is not None:
            pool, self._pool = self._pool, None
            # Closing waits for every connection to come back: a task holding one hangs it.
            try:
                await asyncio.wait_for(pool.close(), COMMAND_TIMEOUT)
            except TimeoutError:
                _log.warning("the engines' pool did not close in %s s: ended", COMMAND_TIMEOUT)
                pool.terminate()
