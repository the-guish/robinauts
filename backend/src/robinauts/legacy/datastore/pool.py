# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Opening the connection pool, and what is deliberately not set on it.

The stores do not open pools: one is made in the composition root, lives as
long as the process does and is handed to each store (``docs/layout.md``).
This is the one place that knows how to make it, so that a command, a test
and the server all open the same kind of pool.

**A pool, not a connection.** Every store method here is one statement on a
connection of its own, and the contract suite of ``CredentialStore`` runs
several at once on purpose: a single shared connection would serialise the
whole server behind one statement, and would break the atomicity the store
promises the moment two callers interleaved on it.

**The session's time zone is not set, on purpose.** ``timestamptz`` travels
as an instant, and asyncpg decodes it to an aware datetime in UTC whatever
the session or the server is set to, so setting one would only hide a bug
rather than prevent one. The tests prove it by running against a server
whose zone is deliberately not UTC.

**It is also where a driver's failure to connect becomes the platform's.**
Opening is the one moment a database answers an operator rather than a
request -- there is no database, the name is wrong, the credentials were
refused, the connection string will not parse -- and every one of those
arrives as an exception of asyncpg's, which nothing above this package may so
much as name (``docs/layout.md``). Left alone it would reach ``robinauts
start`` and ``robinauts db init`` as a traceback about a library. So it is
turned into ``domain.DatabaseUnreachableError`` here, with the driver's own
sentence and **never** the connection string, which is where the password is
(``domain.without_secrets``).
"""

from __future__ import annotations

import asyncpg

from robinauts.legacy.domain import DatabaseUnreachableError

OPENING_FAILURES: tuple[type[BaseException], ...] = (
    # The server answered and said no: no such database, wrong password, too
    # many connections, a role that may not log in.
    asyncpg.PostgresError,
    # The driver would not start: a connection string it cannot read, an
    # option that is not one. ``ClientConfigurationError`` is under this.
    asyncpg.InterfaceError,
    # The machine could not be reached at all: refused, no route, no such
    # name. ``socket.gaierror`` is an ``OSError``.
    OSError,
)
"""What "the database could not be opened" is, in the driver's own terms.

Deliberately the three broad families rather than a list of the codes seen so
far: a code nobody thought of is still an operator's problem, and answering it
with a traceback about asyncpg would be the one case where this helps least.
An error raised **after** a pool is open is not here -- that is a statement
failing, which its caller decides about.
"""

MIN_POOL_SIZE = 2
MAX_POOL_SIZE = 10
"""What a single-process deployment needs: a handful of concurrent statements.

PostgreSQL's own `max_connections` is the limit that matters on the other
side, and a pool larger than the work there is only moves the queue.
"""


async def open_pool(
    dsn: str,
    *,
    min_size: int = MIN_POOL_SIZE,
    max_size: int = MAX_POOL_SIZE,
    command_timeout: float | None = None,
    server_settings: dict[str, str] | None = None,
) -> asyncpg.Pool:
    """A connection pool for ``dsn``, opened on the running event loop.

    ``server_settings`` goes to the driver as it is: it is how a test puts
    itself in a schema of its own, and how a deployment that keeps the
    platform's tables under a named schema says so. ``command_timeout`` is
    the ceiling on a single statement, and is left unset until there is a
    configuration to read it from.

    The caller closes it (``await pool.close()``), on the loop that opened
    it: a pool outliving its loop is a warning at best.

    ``DatabaseUnreachableError`` for every way the database refuses to be
    opened (``OPENING_FAILURES``), carrying what the driver said and not the
    connection string it was given. The original is **chained**, so a log with
    a traceback in it still has the driver's own frames; what a command prints
    is the sentence alone.
    """
    try:
        return await asyncpg.create_pool(
            dsn,
            min_size=min_size,
            max_size=max_size,
            command_timeout=command_timeout,
            server_settings=server_settings,
        )
    except OPENING_FAILURES as refused:
        raise DatabaseUnreachableError.from_driver(refused) from refused
