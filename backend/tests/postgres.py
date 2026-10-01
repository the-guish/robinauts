# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A PostgreSQL for the tests that need one, and a schema of their own in it.

The database is not started here: the tests take the URL of one they are
given, in ``ROBINAUTS_TEST_DATABASE_URL``, and skip with a reason when there
is none, so the plain unit run stays green on a machine with no PostgreSQL.
``CONTRIBUTING.md`` says how to set it; ``DEPENDENCIES.md`` says why no
package that starts a server is a dependency of this project.

**Every test gets a schema of its own**, named after a fresh UUID, and drops
it however it ends. Two things follow. Tests do not interfere: one run's
sweep cannot delete another run's rows, and two runs on one server -- two
terminals, or CI and a laptop against the same database -- pass together.
And the SQL is proved to be schema-agnostic: nothing in ``schema.sql`` or in
the stores names a schema, so the whole thing lands wherever ``search_path``
points.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg
import pytest

from robinauts.legacy.datastore import create_schema, open_pool

DATABASE_URL = os.environ.get("ROBINAUTS_TEST_DATABASE_URL")
"""The PostgreSQL these tests are given, or ``None`` if they were given none."""

REQUIRE_POSTGRES = os.environ.get("ROBINAUTS_REQUIRE_POSTGRES")
"""Whether skipping these tests is allowed. CI sets it; a laptop does not.

A skip is the right answer for a contributor with no PostgreSQL to hand and
the wrong one for a build whose job is to prove the store works: a typo in
the URL, a service container that did not start, a variable dropped from the
workflow, and the database tests quietly stop running while the run stays
green. Set this and "no database" becomes a failure that says so.
"""

NO_DATABASE = (
    "no database: set ROBINAUTS_TEST_DATABASE_URL to a PostgreSQL 14 or later"
    " that these tests may create and drop schemas in"
)

_NOT_REQUIRED = frozenset({"", "0", "false", "no", "off"})


def database_required(required: str | None = REQUIRE_POSTGRES) -> bool:
    """Whether a missing database is a failed run rather than a skipped one.

    Unset, empty, ``0``, ``false``, ``no`` and ``off`` mean no, in any case
    and with any surrounding space; anything else means yes. A workflow that
    writes ``ROBINAUTS_REQUIRE_POSTGRES: "0"`` to turn it off should get what
    it asked for rather than the opposite.
    """
    return required is not None and required.strip().lower() not in _NOT_REQUIRED


def no_database_reason(url: str | None = DATABASE_URL) -> str | None:
    """Why the database tests cannot run, or ``None`` if they can."""
    return None if url else NO_DATABASE


requires_postgres = [
    pytest.mark.io,
    pytest.mark.database,
    pytest.mark.skipif(no_database_reason() is not None, reason=NO_DATABASE),
]
"""What a module of these tests puts in its ``pytestmark``.

``database`` is what ``conftest.py`` counts at the end of a run: it is the
one honest answer to "did the tests that need PostgreSQL actually run", and
a marker is how a test says so without anybody maintaining a list of file
names.

Only ever a skip here. The run is failed instead, when a database is
required, by ``tests/unit/test_database_requirement.py`` and by the session
hook in ``conftest.py`` -- neither of which is one of these modules, so
neither is ever itself skipped.
"""


def database_tests_missing(
    *, required: bool, ran: int, narrowed: bool, distributed: bool = False
) -> str | None:
    """Why this run cannot be believed about the database, or ``None``.

    The variable being set proves that somebody meant the database tests to
    run. It does not prove that any did: ``-m "not io"`` sets it and skips
    them all, and so does a path argument that never reaches them. This is
    what turns "meant to" into "did".

    ``narrowed`` is a run that asked for a subset -- a marker expression, a
    keyword expression, an explicit path. Such a run cannot prove anything
    about the tests it left out, so when a database is required it is
    refused rather than believed. CI never narrows; a laptop that does
    simply does not set the variable.

    ``distributed`` is a run split across processes. The count is kept in
    one process and the tests run in others, so it would always be zero and
    the answer would be a confident lie. Saying "I cannot tell" is the only
    honest thing left; supporting it properly would mean a dependency this
    project does not have.
    """
    if not required:
        return None
    if distributed:
        return (
            "a database is required (ROBINAUTS_REQUIRE_POSTGRES), and this run is split"
            " across processes, where the tests that ran cannot be counted. Run it"
            " without -n, or unset the variable"
        )
    if narrowed:
        return (
            "a database is required (ROBINAUTS_REQUIRE_POSTGRES), but this run was narrowed"
            " with -m, -k or a path, so it cannot show that the database tests ran."
            " Run the whole suite, or unset the variable"
        )
    if ran == 0:
        return (
            "a database is required (ROBINAUTS_REQUIRE_POSTGRES), and not one test marked"
            " `database` ran. The store and the schema were not exercised at all"
        )
    return None


class TemporarySchema:
    """A schema of its own on the test database, and a pool that lives in it.

    ``open`` makes the schema and returns a pool whose every connection has
    ``search_path`` set to it; ``close`` closes the pool and drops the schema
    with everything in it. The pool is opened inside the test's own event
    loop and closed on the same one, which is what the store contract's
    ``new_store`` and ``close_store`` exist for. A test that needs neither
    half separately uses ``temporary_schema`` below.

    The pool holds **several** connections, and ``min_size`` equals
    ``max_size`` so that every one of them is connected before the test
    starts. Both halves matter, and the second is the one that is easy to
    get wrong: the concurrency tests of the store contract start eight calls
    at once, and if the pool were still opening its third connection when
    the first two had finished, the calls would not overlap and the tests
    would pass against a store with no atomicity at all. A warm pool of
    eight is what makes them bite: take the advisory lock out of
    ``add_pending_login`` and the contract's cap test fails against this
    pool -- and passes against a pool that is still opening connections.
    """

    def __init__(
        self,
        *,
        min_size: int = 8,
        max_size: int = 8,
        behind: int = 0,
        server_settings: dict[str, str] | None = None,
    ) -> None:
        self.name = "robinauts_test_" + uuid.uuid4().hex
        self.behind = tuple(f"{self.name}_behind_{n}" for n in range(behind))
        """Further schemas on the search path, after this one.

        Empty unless a test asks for them, and then they are what a second
        application, or a leftover, or somebody else's tables live in: the
        arrangement in which "is the schema there" and "is it the one the
        queries reach" stop being the same question.
        """
        self._min_size = min_size
        self._max_size = max_size
        self._settings = {
            "search_path": ", ".join([self.name, *self.behind]),
            **(server_settings or {}),
        }
        self._pool: asyncpg.Pool | None = None

    @property
    def pool(self) -> asyncpg.Pool:
        """The pool inside this schema; only after ``open`` and before ``close``."""
        if self._pool is None:
            raise RuntimeError("this schema is not open")
        return self._pool

    async def open(self) -> asyncpg.Pool:
        """Create the schema and return a pool that works inside it.

        If anything after the schema exists fails -- a pool that will not
        open, a setting the server refuses -- the schema is dropped again
        before the failure is passed on. Otherwise a bad argument in one test
        would leave a schema behind in everybody's database, and the test
        that reported it would not be the test that made the mess.
        """
        for name in (self.name, *self.behind):
            await self._alone(f'CREATE SCHEMA "{name}"')
        try:
            self._pool = await open_pool(
                _url(),
                min_size=self._min_size,
                max_size=self._max_size,
                server_settings=self._settings,
            )
        except BaseException:
            await self.close()
            raise
        return self._pool

    async def close(self) -> None:
        """Close the pool and drop the schema, whatever the test did to it."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
        for name in (self.name, *self.behind):
            await self._alone(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')

    async def dump(self) -> str:
        """Every column of every row of every table in this schema.

        What the store contract searches for a raw secret. Every table, not
        the ones this test knows about: a secret that ends up in a table
        added later is exactly the one nobody would think to look for.
        """
        lines = []
        async with self.pool.acquire() as connection:
            tables = await connection.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
                " ORDER BY tablename"
            )
            for table in tables:
                name = table["tablename"]
                # The name comes from the catalogue of this test's own schema
                # and is quoted; there is nothing here for a caller to inject.
                rows = await connection.fetch(f'SELECT * FROM "{name}"')
                for row in rows:
                    fields = ", ".join(f"{column}={value!r}" for column, value in row.items())
                    lines.append(f"{name}: {fields}")
        return "\n".join(lines)

    async def _alone(self, statement: str) -> None:
        """Run one statement on a connection of its own, outside any pool.

        Making and dropping the schema cannot go through the pool: the pool
        lives in the schema, and one of the two moments is always before or
        after it.
        """
        connection = await asyncpg.connect(_url())
        try:
            await connection.execute(statement)
        finally:
            await connection.close()


@asynccontextmanager
async def temporary_schema(
    *,
    applied: bool = True,
    behind: int = 0,
    server_settings: dict[str, str] | None = None,
) -> AsyncIterator[TemporarySchema]:
    """A schema of this test's own, dropped however the block ends.

    ``applied`` is whether ``schema.sql`` is applied to it: the tests of the
    schema check itself want a database that has none. ``behind`` is how many
    further schemas to put on the search path after it.
    """
    schema = TemporarySchema(behind=behind, server_settings=server_settings)
    await schema.open()
    try:
        if applied:
            await create_schema(schema.pool)
        yield schema
    finally:
        await schema.close()


async def schema_exists(name: str) -> bool:
    """Whether that schema is still on the test database.

    Asked about one name, never about the pattern: another session's tests
    may be running against the same server, and their schemas are none of
    this one's business.
    """
    connection = await asyncpg.connect(_url())
    try:
        found = await connection.fetchval(
            "SELECT true FROM information_schema.schemata WHERE schema_name = $1", name
        )
        return found is not None
    finally:
        await connection.close()


def _url() -> str:
    """The database URL, or a plain failure rather than a confusing one."""
    if DATABASE_URL is None:
        raise RuntimeError(NO_DATABASE)
    return DATABASE_URL
