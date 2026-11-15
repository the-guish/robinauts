# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The SQLite store makes its schema in a new file, and refuses every file that is not empty or
this build's."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

import pytest

from robinauts.controller.adapters.sqlite.schema import (
    FILE_NAME,
    SCHEMA_SHA256,
    SCHEMA_TABLES,
    schema_sql,
    statements,
)
from robinauts.controller.adapters.sqlite.store import SQLiteStore
from robinauts.controller.contract.domain import ConfigError
from util.aio import asyncio_test

pytestmark = pytest.mark.io


def tables(path: Path) -> list[str]:
    with closing(sqlite3.connect(path)) as connection:
        rows = connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
        return sorted(row[0] for row in rows)


def changed(path: Path, *sql: str) -> None:
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        for statement in sql:
            connection.execute(statement)
    finally:
        connection.close()


async def opened(path: Path) -> None:
    store = SQLiteStore(path)
    await store.open()
    await store.close()


@asyncio_test
async def test_a_new_file_gets_the_whole_schema_and_a_second_open_changes_nothing(
    tmp_path: Path,
) -> None:
    path = tmp_path / FILE_NAME
    await opened(path)
    await opened(path)
    assert tables(path) == sorted(SCHEMA_TABLES)
    with closing(sqlite3.connect(path)) as connection:
        rows = connection.execute("SELECT version, schema_sha256 FROM schema_version").fetchall()
        assert rows == [(1, SCHEMA_SHA256)]
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)


def edit(sql: str) -> Callable[[Path], None]:
    return lambda path: changed(path, sql)


def handmade(path: Path) -> None:
    """The file applied by hand: no hash recorded."""
    changed(path, *statements(schema_sql()))


def foreign(path: Path) -> None:
    changed(path, "CREATE TABLE notes (id INTEGER PRIMARY KEY)")


def unversioned(path: Path) -> None:
    changed(path, "CREATE TABLE sessions (id TEXT PRIMARY KEY)")


def not_a_database(path: Path) -> None:
    path.write_bytes(b"this is not a database, " * 100)


@pytest.mark.parametrize(
    ("made_by", "refusal"),
    [
        (edit("UPDATE schema_version SET version = 2"), "schema version 2, and this build needs 1"),
        (
            edit(f"UPDATE schema_version SET schema_sha256 = '{'0' * 64}'"),
            "another edit of schema.sql",
        ),
        (edit("DROP TABLE turn_events"), r"missing tables \(turn_events\)"),
        (edit("DELETE FROM schema_version"), "no version recorded"),
        (handmade, r"another edit of schema.sql \(None\)"),
    ],
    ids=["another version", "another edit", "a table missing", "no version row", "by hand"],
)
@asyncio_test
async def test_a_file_that_is_not_this_builds_is_refused(
    tmp_path: Path, made_by: Callable[[Path], None], refusal: str
) -> None:
    path = tmp_path / FILE_NAME
    if made_by is not handmade:
        await opened(path)
    made_by(path)
    with pytest.raises(ConfigError, match=refusal):
        await opened(path)


@pytest.mark.parametrize(
    ("made_by", "refusal"),
    [
        (foreign, r"tables that are not Robinauts' \(notes\)"),
        (unversioned, r"tables of ours \(sessions\) with no version"),
        (not_a_database, "could not be opened: file is not a database"),
    ],
    ids=["someone else's", "ours without a version", "not a database"],
)
@asyncio_test
async def test_a_file_that_is_not_empty_is_left_as_it_was(
    tmp_path: Path, made_by: Callable[[Path], None], refusal: str
) -> None:
    path = tmp_path / FILE_NAME
    made_by(path)
    before = path.read_bytes()
    with pytest.raises(ConfigError, match=refusal):
        await opened(path)
    assert path.read_bytes() == before
