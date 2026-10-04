# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Opening the connection pool: one per process, made by the composition, handed to the
store and to the engines.

Every document crosses as JSON text: the ``jsonb`` and ``json`` codecs turn a mapping into
text and back, so no SQL in the store spells JSON, and a number JSON cannot write is
refused here rather than written as ``NaN``. The session's time zone is not set:
``timestamptz`` travels as an instant, and asyncpg hands it back aware, in UTC.

A database that will not open answers an operator, not a request, so the driver's refusal
becomes a ``ConfigError`` carrying the driver's own sentence and never the connection
string, which is where the password is.
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg

from robinauts.controller.contract.domain import ConfigError

MIN_POOL_SIZE = 2
MAX_POOL_SIZE = 10

OPENING_FAILURES: tuple[type[BaseException], ...] = (
    asyncpg.PostgresError,
    asyncpg.InterfaceError,
    OSError,
)


def dumps(value: Any) -> str:
    return json.dumps(value, allow_nan=False, ensure_ascii=False)


async def codecs(connection: asyncpg.Connection) -> None:
    """Documents as mappings on both ``jsonb`` and ``json`` columns."""
    for name in ("jsonb", "json"):
        await connection.set_type_codec(
            name, encoder=dumps, decoder=json.loads, schema="pg_catalog", format="text"
        )


class BoundedPool(asyncpg.Pool):
    """A pool whose every acquire waits at most ``acquire_timeout`` seconds, its own and the
    ones ``fetch``, ``execute`` and the rest make, the engines' included: a pool that stays
    busy answers ``TimeoutError``, which a request turns into a 503, rather than a queue
    nobody leaves."""

    acquire_timeout: float | None = None

    def acquire(self, *, timeout: float | None = None) -> Any:
        return super().acquire(timeout=self.acquire_timeout if timeout is None else timeout)


async def open_pool(
    dsn: str,
    *,
    min_size: int = MIN_POOL_SIZE,
    max_size: int = MAX_POOL_SIZE,
    server_settings: dict[str, str] | None = None,
    acquire_timeout: float | None = None,
) -> asyncpg.Pool:
    """A pool for ``dsn`` on the running loop, with the codecs on every connection; the
    caller closes it on the same loop. ``ConfigError`` when the database will not open."""
    pool = BoundedPool(
        dsn,
        connection_class=asyncpg.Connection,
        record_class=asyncpg.Record,
        min_size=min(min_size, max_size),
        max_size=max_size,
        max_queries=50000,
        max_inactive_connection_lifetime=300.0,
        loop=None,
        init=codecs,
        server_settings=server_settings,
    )
    pool.acquire_timeout = acquire_timeout
    try:
        return await pool
    except OPENING_FAILURES as refused:
        raise ConfigError(f"the database could not be opened: {refused}") from refused
