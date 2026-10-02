# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Whether the tests that need PostgreSQL may skip, and whether they ran.

The database is not started here: the tests take the URL of one they are
given, in ``ROBINAUTS_TEST_DATABASE_URL``, and skip with a reason when there
is none, so the plain unit run stays green on a machine with no PostgreSQL.
``CONTRIBUTING.md`` says how to set it; ``DEPENDENCIES.md`` says why no
package that starts a server is a dependency of this project.
"""

from __future__ import annotations

import os

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
