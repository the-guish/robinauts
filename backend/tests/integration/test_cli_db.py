# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""``robinauts db init`` against a real PostgreSQL.

The command is what creates a deployment's schema -- the server never does
(``docs/specs/backend.md``) -- so what it is worth is what it does to a
database: an empty one gets the whole schema, one that already has this
version is left exactly as it is, and one that has some other version is
refused, because there are no migrations yet and applying the file over it
would relabel rather than upgrade.

The rules themselves are ``datastore.create_schema``'s and are tested in
``test_postgres_schema.py``. What is tested here is the **command**: that it
reads the database out of the environment, that it exits 0 when the database
is as it should be, and that it prints a refusal and exits 1 rather than a
traceback when it is not.

Each test works in a schema of its own, named in the connection string the way
a deployment would name one.

Marked ``io`` and ``database``; skipped, with the reason, without
``ROBINAUTS_TEST_DATABASE_URL``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from aio import asyncio_test
from postgres import DATABASE_URL, TemporarySchema
from postgres import requires_postgres as requires_postgres_marks
from robinauts.legacy import cli
from robinauts.legacy.app import DATABASE_URL_VARIABLE
from robinauts.legacy.datastore import SCHEMA_VERSION, check_schema, schema_version
from robinauts.legacy.domain import DB_INIT_COMMAND

pytestmark = requires_postgres_marks

OTHER_VERSION = SCHEMA_VERSION + 1
"""A version this build cannot read, which is what a refusal is about."""

_A_VERSION_ROW = f"""
CREATE TABLE schema_version (
    version integer PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO schema_version (version) VALUES ({OTHER_VERSION});
"""
"""A database somebody else's Robinauts made: a version, and nothing to read it."""


@pytest.fixture(autouse=True)
def _the_log_is_put_back() -> Iterator[None]:
    """A command configures logging for a whole process; a test is not one."""
    root = logging.getLogger()
    held, level = list(root.handlers), root.level
    try:
        yield
    finally:
        root.handlers[:] = held
        root.setLevel(level)


def in_schema(name: str) -> str:
    """The test database's URL, with the search path a deployment would set."""
    assert DATABASE_URL is not None
    separator = "&" if "?" in DATABASE_URL else "?"
    return f"{DATABASE_URL}{separator}options=-csearch_path%3D{name}"


@asynccontextmanager
async def schema() -> AsyncIterator[TemporarySchema]:
    """An empty schema of this test's own, dropped however the block ends."""
    temporary = TemporarySchema(min_size=1, max_size=1)
    await temporary.open()
    try:
        yield temporary
    finally:
        await temporary.close()


async def initialised(monkeypatch: Any, url: str) -> int:
    """Run ``robinauts db init`` against ``url``, as a shell would.

    In a thread of its own, because the command opens a loop with
    ``asyncio.run`` -- which is what a command does and what a test may not do
    from inside the loop it is already running on.
    """
    monkeypatch.setenv(DATABASE_URL_VARIABLE, url)
    return await asyncio.to_thread(cli.run, ["db", "init"])


@asyncio_test
async def test_an_empty_database_gets_the_schema(monkeypatch: Any) -> None:
    async with schema() as temporary:
        code = await initialised(monkeypatch, in_schema(temporary.name))

        assert code == cli.OK
        assert await schema_version(temporary.pool) == SCHEMA_VERSION
        # And it is a database this build agrees to serve, which is the only
        # thing the command is for.
        await check_schema(temporary.pool)


@asyncio_test
async def test_running_it_again_is_a_success_that_changes_nothing(monkeypatch: Any) -> None:
    # An operator who is not sure whether it has been run may simply run it.
    async with schema() as temporary:
        assert await initialised(monkeypatch, in_schema(temporary.name)) == cli.OK
        applied = await temporary.pool.fetchval("SELECT applied_at FROM schema_version")

        again = await initialised(monkeypatch, in_schema(temporary.name))

        assert again == cli.OK
        assert await temporary.pool.fetchval("SELECT count(*) FROM schema_version") == 1
        assert await temporary.pool.fetchval("SELECT applied_at FROM schema_version") == applied


@asyncio_test
async def test_a_database_of_another_version_is_refused(monkeypatch: Any, capsys: Any) -> None:
    async with schema() as temporary:
        await temporary.pool.execute(_A_VERSION_ROW)

        code = await initialised(monkeypatch, in_schema(temporary.name))

        said = capsys.readouterr().err
        assert code == cli.FAILED
        assert "Traceback" not in said
        assert str(OTHER_VERSION) in said
        # And it says what to do, which for a schema with no migrations is to
        # make the database again.
        assert DB_INIT_COMMAND in said
        assert await schema_version(temporary.pool) == OTHER_VERSION


@asyncio_test
async def test_a_database_made_from_an_older_file_is_refused(monkeypatch: Any, capsys: Any) -> None:
    # What a developer sees after pulling a schema change: the version is
    # still 1, so it is the hash that says the database is to be made again.
    async with schema() as temporary:
        assert await initialised(monkeypatch, in_schema(temporary.name)) == cli.OK
        await temporary.pool.execute("UPDATE schema_version SET schema_sha256 = $1", "0" * 64)

        code = await initialised(monkeypatch, in_schema(temporary.name))

        said = capsys.readouterr().err
        assert code == cli.FAILED
        assert "Traceback" not in said
        assert "made from an older schema.sql" in said
        assert "drop it and run" in said
        assert DB_INIT_COMMAND in said


@asyncio_test
async def test_a_database_that_is_not_there_is_a_failure_and_not_a_traceback(
    monkeypatch: Any, capsys: Any
) -> None:
    # A url that names a host nothing answers on: an operator's mistake, and
    # the one this command is most often run with.
    code = await initialised(monkeypatch, "postgresql://robinauts@127.0.0.1:1/robinauts")

    said = capsys.readouterr().err
    assert code == cli.FAILED
    assert "Traceback" not in said
    assert said.startswith(f"{cli.PROGRAM}: ")


@asyncio_test
async def test_a_database_name_that_is_nobodys_is_one_line_and_not_a_traceback(
    monkeypatch: Any, capsys: Any
) -> None:
    """The real server, refusing for real: ``InvalidCatalogNameError``.

    The commonest of an operator's mistakes -- running the command before the
    database is created -- and the one that used to arrive as a traceback about
    asyncpg. The translation is ``datastore.open_pool``'s; this is it happening
    against PostgreSQL rather than against a stand-in.
    """
    assert DATABASE_URL is not None
    nobodys = DATABASE_URL.rsplit("/", 1)[0] + "/robinauts_no_such_database"

    code = await initialised(monkeypatch, nobodys)

    said = capsys.readouterr().err
    assert code == cli.FAILED
    assert "Traceback" not in said
    assert len(said.strip().splitlines()) == 1
    assert "robinauts_no_such_database" in said
    # The sentence, and not the url it was in.
    assert nobodys not in said


@pytest.mark.parametrize("argv", [["db"], ["db", "init", "--url", "x"]])
def test_the_command_takes_no_database_on_its_command_line(argv: list[str]) -> None:
    # One place a deployment's database is named, and it is the environment
    # (docs/specs/operations.md): a url on a command line is a password in a
    # shell history.
    with pytest.raises(SystemExit) as raised:
        cli.parser().parse_args(argv)

    assert raised.value.code == cli.MISUSED
