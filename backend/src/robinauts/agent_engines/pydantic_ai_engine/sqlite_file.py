# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The engine's SQLite file: opening it, applying or checking its schema, and running each
operation on the one thread that holds the connection.

The LangChain engine has a copy of this module: no engine imports another, and the controller's
SQLite adapter is out of an engine's reach. The three make the same choices:

- **One connection on one thread.** The standard library's ``sqlite3`` blocks, so one thread of
  the file's own holds the connection and runs every operation, one after the other, each in a
  transaction: ``BEGIN IMMEDIATE`` to write, ``BEGIN`` to read.
- **Write-ahead logging.** A crash leaves the last commit or the one before it. Readers do not
  block the writer.
- **``synchronous = FULL``.** Each commit reaches the disk before it returns.
- **A busy timeout.** A write waits up to ``BUSY_TIMEOUT`` seconds for a lock another process
  holds, then raises ``TimeoutError``.
- **Foreign keys on.**
- **A schema version.** The file records the version and the hash of the schema it was made
  from. A new file gets the schema in one transaction. A file of another version, of another
  edit of the schema, of someone else's, or that is not a database is refused with
  ``StorageRefusedError`` and left as it was.

The folder is made readable by this user alone, and so is the file.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sqlite3
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from robinauts.agent_engines.contract.domain import StorageRefusedError

BUSY_TIMEOUT = 10.0
"""The seconds a statement waits for a lock another process holds."""

MINIMUM_SQLITE = (3, 37, 0)
"""The first SQLite with ``STRICT`` tables."""

START_AGAIN = "move it away and start again"

VERSION_TABLE = """
CREATE TABLE schema_version (
    version integer NOT NULL,
    schema_sha256 text NOT NULL
) STRICT;
"""


class SQLiteFile:
    """The file at ``path``, with ``schema``: ``version`` and the tables it makes, ``tables``.

    The first operation opens the file and applies or checks the schema; ``close`` lets go."""

    def __init__(
        self,
        path: Path,
        *,
        schema: str,
        version: int,
        tables: frozenset[str],
        busy_timeout: float = BUSY_TIMEOUT,
    ) -> None:
        self.path = path
        self._schema = schema
        self._version = version
        self._digest = hashlib.sha256(schema.encode("utf-8")).hexdigest()
        self._tables = tables | {"schema_version"}
        self._busy_timeout = busy_timeout
        self._thread: ThreadPoolExecutor | None = None
        self._opening: Future[None] | None = None
        self._connection: sqlite3.Connection | None = None

    async def write[T](self, work: Callable[[sqlite3.Connection], T]) -> T:
        """``work`` in a transaction that holds the write lock from its first statement."""
        return await self._run(work, "BEGIN IMMEDIATE")

    async def read[T](self, work: Callable[[sqlite3.Connection], T]) -> T:
        return await self._run(work, "BEGIN")

    async def open(self) -> None:
        """Open the file and apply or check its schema. Safe to repeat."""
        await self._run(lambda _: None, "BEGIN")

    async def close(self) -> None:
        if self._thread is None:
            return
        thread, self._thread, self._opening = self._thread, None, None
        try:
            await asyncio.get_running_loop().run_in_executor(thread, self._disconnect)
        finally:
            thread.shutdown(wait=True)

    async def _run[T](self, work: Callable[[sqlite3.Connection], T], begin: str) -> T:
        if self._thread is None:
            self._thread = ThreadPoolExecutor(max_workers=1, thread_name_prefix="engine-sqlite")
            self._opening = self._thread.submit(self._connect)
        thread, opening = self._thread, self._opening
        assert opening is not None
        try:
            # Shielded: a caller cancelled meanwhile leaves the opening to the next one.
            await asyncio.shield(asyncio.wrap_future(opening))
        except Exception:
            # The next operation tries again: the file may have been moved away meanwhile.
            if self._thread is thread:
                self._thread = self._opening = None
                thread.shutdown(wait=False)
            raise
        return await asyncio.get_running_loop().run_in_executor(
            thread, self._transaction, work, begin
        )

    def _transaction[T](self, work: Callable[[sqlite3.Connection], T], begin: str) -> T:
        connection = self._connection
        if connection is None:
            raise RuntimeError(f"the engine's file {self.path} is not open")
        try:
            connection.execute(begin)
            try:
                result = work(connection)
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        except sqlite3.OperationalError as failed:
            if getattr(failed, "sqlite_errorname", "") in ("SQLITE_BUSY", "SQLITE_LOCKED"):
                raise TimeoutError(f"the engine's file {self.path} is locked: {failed}") from failed
            raise
        return result

    def _connect(self) -> None:
        if sqlite3.sqlite_version_info < MINIMUM_SQLITE:
            raise StorageRefusedError(
                f"this Python's SQLite is {sqlite3.sqlite_version}, and the engine needs"
                f" {'.'.join(map(str, MINIMUM_SQLITE))} or later"
            )
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            # SQLite gives the log files the mode of the database file.
            os.close(os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600))
            connection = sqlite3.connect(
                self.path, timeout=self._busy_timeout, isolation_level=None
            )
        except (OSError, sqlite3.Error) as refused:
            raise StorageRefusedError(
                f"the engine's file {self.path} could not be opened: {refused}"
            ) from refused
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA synchronous = FULL")
            self._look(connection, write=False)
            mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if mode != "wal":
                raise StorageRefusedError(
                    f"the engine's file {self.path} cannot use write-ahead logging"
                    f" (journal mode {mode})"
                )
            # Looked at again under the write lock: another process may have made it meanwhile.
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._look(connection, write=True)
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        except sqlite3.DatabaseError as refused:
            connection.close()
            raise StorageRefusedError(
                f"the engine's file {self.path} could not be opened: {refused}"
            ) from refused
        except BaseException:
            connection.close()
            raise
        self._connection = connection

    def _disconnect(self) -> None:
        if self._connection is not None:
            connection, self._connection = self._connection, None
            connection.close()

    def _look(self, connection: sqlite3.Connection, *, write: bool) -> None:
        """Refuse a file that is not empty or this build's; make the schema in an empty one
        when ``write``."""
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if not names:
            if write:
                for statement in _statements(VERSION_TABLE + self._schema):
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_version (version, schema_sha256) VALUES (?, ?)",
                    (self._version, self._digest),
                )
            return
        if "schema_version" not in names:
            raise StorageRefusedError(
                f"the engine's file {self.path} holds tables that are not this engine's"
                f" ({', '.join(sorted(names))}): {START_AGAIN}"
            )
        try:
            row = connection.execute("SELECT version, schema_sha256 FROM schema_version").fetchone()
        except sqlite3.OperationalError as unreadable:
            raise StorageRefusedError(
                f"the schema_version table of {self.path} is not this engine's: {START_AGAIN}"
            ) from unreadable
        if row is None:
            raise StorageRefusedError(
                f"the engine's file {self.path} has no version recorded: {START_AGAIN}"
            )
        found, recorded = row
        if found != self._version:
            raise StorageRefusedError(
                f"the engine's file {self.path} is at schema version {found}, and this build"
                f" needs {self._version}: there is no migration yet, so {START_AGAIN}"
            )
        if recorded != self._digest:
            raise StorageRefusedError(
                f"the engine's file {self.path} was made from another edit of its schema:"
                f" {START_AGAIN}"
            )
        if names != self._tables:
            raise StorageRefusedError(
                f"the engine's file {self.path} holds the tables"
                f" {', '.join(sorted(names))}, not this engine's: {START_AGAIN}"
            )


def _statements(sql: str) -> list[str]:
    """The statements of ``sql`` one by one. A statement ends at the end of a line."""
    found, pending = [], ""
    for line in sql.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            found.append(pending)
            pending = ""
    return found
