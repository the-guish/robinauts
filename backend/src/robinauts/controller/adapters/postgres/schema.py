# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The schema: the file that defines it, applying it, and checking it.

``schema.sql`` beside this module is the controller's whole schema, shipped in the wheel.
A command applies it (``robinauts db init``); the server only checks what it finds and
refuses a database that is not this build's (``docs/deployment.md``). Until the first
release the version stays 1 and the file is edited in place, so the hash the version row
records is what tells an older edit apart: ``SCHEMA_SHA256`` is pinned here and a test
fails when the file changes and the pin does not.

Everything is asked of ``current_schema()``: the first schema on the connection's search
path that exists, where the unqualified statements of ``schema.sql`` and of the store
land. A name of ours that a schema ahead of it answers to would take the store's writes,
so that is refused as a connection to set up differently, not a database to make.

``create_schema`` looks before it writes, under an advisory lock on the schema, so that
two commands at once make one schema and one no-op rather than a collision inside
PostgreSQL's catalogue. It never changes a database it did not make: ``CREATE TABLE IF
NOT EXISTS`` over an older table would relabel it, not upgrade it.

A refusal is a ``ConfigError`` that says what was found and what to do.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from importlib import resources

import asyncpg

from robinauts.controller.contract.domain import ConfigError

SCHEMA_VERSION = 1
"""The schema this build was written against; frozen at 1 until the first release."""

SCHEMA_SHA256 = "a20aad0e19c6a713209e414da27d1e7e4833e25e4dd5a111b18fc15704e5c3de"
"""``schema.sql`` as this build was written against it, line endings normalised to LF."""

SCHEMA_TABLES = (
    "api_tokens",
    "messages",
    "pending_logins",
    "schema_version",
    "sessions",
    "turn_events",
    "turns",
    "user_sessions",
    "users",
)
"""Every table ``schema.sql`` creates; a test keeps this list in step with it."""

COMMAND = "robinauts db init"

_LOCK_SPACE = 1382508616
"""The high half of the advisory lock key: ours, and nobody else's, in a space shared with
every program on the database."""

Executor = asyncpg.Pool | asyncpg.Connection

_LOCK_SCHEMA = f"""
SELECT pg_advisory_xact_lock(
    ({_LOCK_SPACE}::bigint << 32)
    | (SELECT oid FROM pg_namespace WHERE nspname = current_schema())::bigint
)
"""

_TABLES_HERE = """
SELECT name,
       (
           SELECT c.oid
           FROM pg_class AS c
           JOIN pg_namespace AS n ON n.oid = c.relnamespace
           WHERE n.nspname = current_schema()
             AND c.relname = name
             AND c.relkind IN ('r', 'p')
       ) AS here,
       (
           SELECT array_position(current_schemas(true), n.nspname)
                < array_position(current_schemas(true), current_schema())
           FROM pg_class AS c
           JOIN pg_namespace AS n ON n.oid = c.relnamespace
           WHERE c.oid = to_regclass(quote_ident(name))
       ) AS in_front
FROM unnest($1::text[]) AS name
"""
"""For each table name: the table in ``current_schema()``, and whether a relation of that
name in a schema ahead of it would take an unqualified statement instead."""


def schema_sql() -> str:
    """The text of ``schema.sql``, read from the installed package."""
    return resources.files(__package__).joinpath("schema.sql").read_text(encoding="utf-8")


async def create_schema(executor: Executor) -> None:
    """Create the schema in an empty database, or raise ``ConfigError``.

    Safe to repeat: a database at this version, made from this file, is left alone. A
    database of another version, with our tables and no version, made from another edit
    of the file, or missing a table, is refused: there is no migration to run.
    """
    await _on_a_connection(executor, _create_schema)


async def check_schema(executor: Executor) -> None:
    """Raise ``ConfigError`` unless the database is the one this build knows: UTF8, a
    search path naming a schema, the version and the hash recorded, every table there and
    nothing ahead of it answering to their names."""
    await _on_a_connection(executor, _check_schema)


def check_encoding(encoding: str) -> None:
    """A cluster made under ``LANG=C`` is ``SQL_ASCII``, and takes no non-ASCII document."""
    if encoding.upper() != "UTF8":
        raise ConfigError(
            f"the database's encoding is {encoding}, not UTF8, so it cannot hold what a"
            f" conversation says: make it with `createdb -E UTF8` and run `{COMMAND}`"
        )


async def _on_a_connection[T](
    executor: Executor, work: Callable[[asyncpg.Connection], Awaitable[T]]
) -> T:
    """Every question here is about one connection's schema, path and lock."""
    if isinstance(executor, asyncpg.Pool):
        async with executor.acquire() as connection:
            return await work(connection)
    return await work(executor)


async def _create_schema(connection: asyncpg.Connection) -> None:
    check_encoding(await connection.fetchval("SELECT current_setting('server_encoding')"))
    async with connection.transaction():
        await connection.execute(_LOCK_SCHEMA)
        tables, found, recorded = await _state(connection)
        if found is None:
            if tables.here:
                raise _unversioned(tables.here)
            tables.check_nothing_is_in_the_way()
            await connection.execute(schema_sql())
            # The file cannot hold its own hash, and the row is no claim until it is in.
            await connection.execute("UPDATE schema_version SET schema_sha256 = $1", SCHEMA_SHA256)
            return
        _check_recorded(found, recorded)
        tables.check()


async def _check_schema(connection: asyncpg.Connection) -> None:
    check_encoding(await connection.fetchval("SELECT current_setting('server_encoding')"))
    tables, found, recorded = await _state(connection)
    if found is None:
        raise _unversioned(tables.here) if tables.here else _missing()
    _check_recorded(found, recorded)
    tables.check()


def _check_recorded(found: int, recorded: str | None) -> None:
    if found != SCHEMA_VERSION:
        raise ConfigError(
            f"the database is at schema version {found}, and this build needs"
            f" {SCHEMA_VERSION}: there is no migration yet, so drop it and run `{COMMAND}`"
        )
    if recorded != SCHEMA_SHA256:
        raise ConfigError(
            "the database was made from another edit of schema.sql"
            f" ({'applied by hand, no hash recorded' if recorded is None else recorded}),"
            f" not this build's ({SCHEMA_SHA256}): drop it and run `{COMMAND}`"
        )


def _missing() -> ConfigError:
    return ConfigError(f"the database has no schema: run `{COMMAND}`")


def _unversioned(names: list[str]) -> ConfigError:
    return ConfigError(
        f"the database holds tables of ours ({', '.join(sorted(names))}) with no version"
        f" recorded: drop them and run `{COMMAND}`"
    )


async def _state(connection: asyncpg.Connection) -> tuple[_Tables, int | None, str | None]:
    here = await _current_schema(connection)
    tables = _Tables(await connection.fetch(_TABLES_HERE, list(SCHEMA_TABLES)))
    if "schema_version" not in tables.here:
        return tables, None, None
    try:
        row = await connection.fetchrow(
            "SELECT version, to_jsonb(row) ->> 'schema_sha256' AS recorded"
            f" FROM {here}.schema_version AS row"
        )
    except asyncpg.exceptions.UndefinedColumnError as unreadable:
        raise ConfigError(
            f"the database's schema_version table is not this build's: drop it and run"
            f" `{COMMAND}`"
        ) from unreadable
    if row is None:
        return tables, None, None
    found = row["version"]
    if not isinstance(found, int):
        raise ConfigError(f"the database's schema version is not a number: run `{COMMAND}`")
    return tables, found, row["recorded"]


class _Tables:
    def __init__(self, rows: list[asyncpg.Record]) -> None:
        self.here = [row["name"] for row in rows if row["here"] is not None]
        self.in_front = [row["name"] for row in rows if row["in_front"]]

    def check(self) -> None:
        missing = set(SCHEMA_TABLES) - set(self.here)
        if missing:
            raise ConfigError(
                f"the database is missing tables ({', '.join(sorted(missing))}): a half-applied"
                f" schema.sql, so drop it and run `{COMMAND}`"
            )
        self.check_nothing_is_in_the_way()

    def check_nothing_is_in_the_way(self) -> None:
        if self.in_front:
            raise ConfigError(
                f"the connection's search path reaches {', '.join(sorted(self.in_front))} in a"
                " schema ahead of its own: set the search path to this deployment's schema"
            )


async def _current_schema(connection: asyncpg.Connection) -> str:
    row = await connection.fetchrow(
        "SELECT quote_ident(current_schema()) AS here, current_setting('search_path') AS path"
    )
    if row["here"] is None:
        raise ConfigError(
            f"the connection's search path ({row['path']}) names no schema that exists"
        )
    return row["here"]
