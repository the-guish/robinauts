# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The controller's ``schema.sql`` is pinned by its hash, and its tables are listed."""

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
