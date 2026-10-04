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

Every acquire from the pool is bounded, whoever asks, the engines included: past
``acquire_timeout`` it raises ``TimeoutError`` rather than queue for ever behind a pool that
is all in use. The pool is one of three kinds of connection a process holds: the listener and
the work connection are each one more, apart from it.
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg

from robinauts.controller.contract.domain import ConfigError

MIN_POOL_SIZE = 2
MAX_POOL_SIZE = 10
ACQUIRE_TIMEOUT = 5.0

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


class _BoundedPool(asyncpg.Pool):
    """A pool whose every acquire waits at most ``acquire_timeout``, unless told otherwise:
    ``fetchval`` and the rest acquire through it too."""

    def __init__(self, *args: Any, acquire_timeout: float, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.acquire_timeout = acquire_timeout

    def acquire(self, *, timeout: float | None = None) -> Any:
        return super().acquire(timeout=self.acquire_timeout if timeout is None else timeout)


async def open_pool(
    dsn: str,
    *,
    min_size: int = MIN_POOL_SIZE,
    max_size: int = MAX_POOL_SIZE,
    acquire_timeout: float = ACQUIRE_TIMEOUT,
    server_settings: dict[str, str] | None = None,
) -> asyncpg.Pool:
    """A pool for ``dsn`` on the running loop, with the codecs on every connection and every
    acquire bounded; the caller closes it on the same loop. ``ConfigError`` when the database
    will not open."""
    try:
        return await _BoundedPool(
            dsn,
            min_size=min(min_size, max_size),
            max_size=max_size,
            acquire_timeout=acquire_timeout,
            max_queries=50000,
            max_inactive_connection_lifetime=300.0,
            loop=None,
            connection_class=asyncpg.Connection,
            record_class=asyncpg.Record,
            server_settings=server_settings,
            init=codecs,
        )
    except OPENING_FAILURES as refused:
        raise ConfigError(f"the database could not be opened: {refused}") from refused
