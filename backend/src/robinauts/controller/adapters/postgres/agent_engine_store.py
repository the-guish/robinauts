# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The agent engines' store on PostgreSQL: a pool of the worker's own, which it hands the engines
as their handle, and nothing else. Not a ``PostgresStore``: a worker holds no operation on the
core's tables, which it reaches through its ``TurnBroker`` alone.

The schema is not checked here: the server that reads the core's tables checks it, and the
engines' tables are the ones ``robinauts db init`` set up.
"""

from __future__ import annotations

import asyncpg

from robinauts.controller.adapters.postgres.pool import close_pool, open_pool
from robinauts.controller.ports.agent_engine_store import AgentEngineStore


class PostgresAgentEngineStore(AgentEngineStore):
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
            await close_pool(pool)
