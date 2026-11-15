# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The controller's ``schema.sql`` files, PostgreSQL's and SQLite's, are pinned by their hash,
and their tables are listed."""

from __future__ import annotations

import hashlib
import re

import pytest

from robinauts.controller.adapters.postgres.schema import (
    SCHEMA_SHA256,
    SCHEMA_TABLES,
    SCHEMA_VERSION,
    check_encoding,
    schema_sql,
)
from robinauts.controller.adapters.sqlite import schema as sqlite
from robinauts.controller.contract.domain import ConfigError


def test_the_pin_is_the_file() -> None:
    digest = hashlib.sha256(schema_sql().replace("\r\n", "\n").encode("utf-8")).hexdigest()
    assert digest == SCHEMA_SHA256, (
        "schema.sql changed: update SCHEMA_SHA256 in controller/adapters/postgres/schema.py"
        f" to {digest}"
    )


def test_the_version_is_one_until_the_first_release() -> None:
    assert SCHEMA_VERSION == 1
    assert "INSERT INTO schema_version (version) VALUES (1)" in schema_sql()


def test_the_tables_listed_are_the_tables_the_file_creates() -> None:
    created = re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", schema_sql())
    assert sorted(created) == sorted(SCHEMA_TABLES)


def test_only_utf8_passes() -> None:
    check_encoding("UTF8")
    with pytest.raises(ConfigError, match="SQL_ASCII, not UTF8"):
        check_encoding("SQL_ASCII")


def test_the_sqlite_pin_is_its_file() -> None:
    digest = sqlite.schema_digest(sqlite.schema_sql())
    assert digest == sqlite.SCHEMA_SHA256, (
        "sqlite/schema.sql changed: update SCHEMA_SHA256 in controller/adapters/sqlite/schema.py"
        f" to {digest}"
    )


def test_the_sqlite_version_is_one_and_its_tables_are_the_stores_alone() -> None:
    assert sqlite.SCHEMA_VERSION == 1
    assert "INSERT INTO schema_version (only_row, version) VALUES (1, 1)" in sqlite.schema_sql()
    created = re.findall(r"CREATE TABLE (\w+)", sqlite.schema_sql())
    assert sorted(created) == sorted(sqlite.SCHEMA_TABLES)
    # Sign-in's tables are oneberth's, in its own store, on both.
    assert set(sqlite.SCHEMA_TABLES) == set(SCHEMA_TABLES)


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT 1;", ["SELECT 1;"]),
        (
            "-- a; comment\nSELECT 1;\nSELECT 2;\n",
            ["-- a; comment\nSELECT 1;\n", "SELECT 2;\n"],
        ),
        (
            "CREATE TABLE t (\n  a TEXT -- ;\n);\n",
            ["CREATE TABLE t (\n  a TEXT -- ;\n);\n"],
        ),
        ("SELECT 'a;\nb';\n", ["SELECT 'a;\nb';\n"]),
        ("-- nothing\n", []),
    ],
)
def test_the_sqlite_file_is_applied_statement_by_statement(sql: str, expected: list[str]) -> None:
    assert list(sqlite.statements(sql)) == expected
