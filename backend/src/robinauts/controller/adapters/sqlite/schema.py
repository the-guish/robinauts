# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The SQLite file: opening it, and applying or checking ``schema.sql`` beside this module.

The backend's process alone opens the file, so the store applies the schema itself: to a file
that is new or empty, in one transaction. A file at this version, made from this edit of
``schema.sql``, is used as it is. Every other file is refused with a ``ConfigError`` that says
what was found: another version, another edit, tables of ours without a version, tables of
someone else's, or a file that is not a database. Until the first release the version stays 1
and the file is edited in place, so the hash recorded beside the version tells an older edit
apart, as on PostgreSQL. ``SCHEMA_SHA256`` is pinned here, and a test fails when the file
changes and the pin does not.

A connection is set up for a crash:

- **Write-ahead logging.** A commit is an append to the log, and a crash at any moment leaves
  the last commit or the one before it, never a torn page. Readers do not block the writer.
- **``synchronous = FULL``.** Each commit reaches the disk before it returns, so a power cut
  keeps every commit that returned. PostgreSQL does the same with its default settings.
- **A busy timeout.** Another process that holds the write lock, such as a backup or a shell
  on the file, makes a write wait up to ``BUSY_TIMEOUT`` seconds. Past it the write fails,
  and the store raises ``TimeoutError``, as a query on the PostgreSQL pool does past its
  timeout.
- **Foreign keys on**, which SQLite leaves off on every new connection.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from collections.abc import Iterator
from importlib import resources
from pathlib import Path

from robinauts.controller.contract.domain import ConfigError

FILE_NAME = "robinauts.db"
"""The store's file in the data directory."""

SCHEMA_VERSION = 1
"""The schema this build was written against; frozen at 1 until the first release."""

SCHEMA_SHA256 = "95342fa71ca5e71a3d9476182546d49b1dc1eb483a0cdc62e4cb205fa0dd32f0"
"""``schema.sql`` as this build was written against it, line endings normalised to LF."""

SCHEMA_TABLES = (
    "messages",
    "projects",
    "schema_version",
    "sessions",
    "tasks",
    "turn_events",
    "turns",
    "users",
)
"""Every table ``schema.sql`` creates; a test keeps this list in step with it."""

BUSY_TIMEOUT = 10.0
"""The seconds a statement waits for a lock another process holds, the PostgreSQL pool's
command timeout: under one heartbeat (30 s) and well under the lease (90 s)."""

MINIMUM_SQLITE = (3, 37, 0)
"""The first SQLite with ``STRICT`` tables."""

START_AGAIN = "move it away and start again"


def schema_sql() -> str:
    """The text of ``schema.sql``, read from the installed package."""
    return resources.files(__package__).joinpath("schema.sql").read_text(encoding="utf-8")


def schema_digest(text: str) -> str:
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def connect(path: Path, *, busy_timeout: float = BUSY_TIMEOUT) -> sqlite3.Connection:
    """A connection to the file at ``path``, made with its folder if they are missing, readable
    by this user alone. Autocommit: the caller begins and ends each transaction.
    ``create_or_check`` turns write-ahead logging on, once it knows the file is ours.
    ``ConfigError`` for a file that cannot be opened."""
    if sqlite3.sqlite_version_info < MINIMUM_SQLITE:
        raise ConfigError(
            f"this Python's SQLite is {sqlite3.sqlite_version}, and the store needs"
            f" {'.'.join(map(str, MINIMUM_SQLITE))} or later"
        )
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # SQLite gives the log files the mode of the database file.
        os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o600))
        connection = sqlite3.connect(
            path, timeout=busy_timeout, isolation_level=None, check_same_thread=True
        )
    except (OSError, sqlite3.Error) as refused:
        raise ConfigError(f"the database file {path} could not be opened: {refused}") from refused
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
    except sqlite3.DatabaseError as refused:
        connection.close()
        raise ConfigError(f"the database file {path} could not be opened: {refused}") from refused
    return connection


def create_or_check(connection: sqlite3.Connection, path: Path) -> None:
    """Apply the schema to an empty file, or check that the file is this build's: else
    ``ConfigError``, and the file is left as it was. Safe to repeat, and safe against a second
    process doing the same."""
    try:
        _look(connection, path, write=False)
        mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if mode != "wal":
            raise ConfigError(
                f"the database file {path} cannot use write-ahead logging (journal mode {mode})"
            )
        # Looked at again under the write lock: another process may have made it meanwhile.
        connection.execute("BEGIN IMMEDIATE")
        try:
            _look(connection, path, write=True)
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
    except sqlite3.DatabaseError as refused:
        raise ConfigError(f"the database file {path} could not be opened: {refused}") from refused


def _look(connection: sqlite3.Connection, path: Path, *, write: bool) -> None:
    """Refuse a file that is not empty or this build's; make the schema in an empty one when
    ``write``."""
    names = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    if "schema_version" in names:
        _check(connection, path, names)
    else:
        _refuse_unless_empty(path, names)
        if write:
            _create(connection)


def statements(sql: str) -> Iterator[str]:
    """The statements of ``sql`` one by one, each with the comments before it. A statement
    ends at the end of a line."""
    pending = ""
    for line in sql.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            yield pending
            pending = ""


def _refuse_unless_empty(path: Path, names: set[str]) -> None:
    ours = names & set(SCHEMA_TABLES)
    if ours:
        raise ConfigError(
            f"the database file {path} holds tables of ours ({', '.join(sorted(ours))}) with no"
            f" version recorded: {START_AGAIN}"
        )
    if names:
        raise ConfigError(
            f"the database file {path} holds tables that are not Robinauts'"
            f" ({', '.join(sorted(names))}): name another data directory"
        )


def _create(connection: sqlite3.Connection) -> None:
    for statement in statements(schema_sql()):
        connection.execute(statement)
    # The file cannot hold its own hash, and the row is no claim until it is in.
    connection.execute("UPDATE schema_version SET schema_sha256 = ?", (SCHEMA_SHA256,))


def _check(connection: sqlite3.Connection, path: Path, names: set[str]) -> None:
    try:
        row = connection.execute("SELECT version, schema_sha256 FROM schema_version").fetchone()
    except sqlite3.OperationalError as unreadable:
        raise ConfigError(
            f"the schema_version table of {path} is not this build's: {START_AGAIN}"
        ) from unreadable
    if row is None:
        raise ConfigError(f"the database file {path} has no version recorded: {START_AGAIN}")
    found, recorded = row
    if found != SCHEMA_VERSION:
        raise ConfigError(
            f"the database file {path} is at schema version {found}, and this build needs"
            f" {SCHEMA_VERSION}: there is no migration yet, so {START_AGAIN}"
        )
    if recorded != SCHEMA_SHA256:
        raise ConfigError(
            f"the database file {path} was made from another edit of schema.sql ({recorded}),"
            f" not this build's ({SCHEMA_SHA256}): {START_AGAIN}"
        )
    missing = set(SCHEMA_TABLES) - names
    if missing:
        raise ConfigError(
            f"the database file {path} is missing tables ({', '.join(sorted(missing))}):"
            f" {START_AGAIN}"
        )
