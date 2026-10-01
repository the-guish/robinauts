# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What a database that will not open says, and what it must never say.

Opening is the one moment a database answers an **operator** rather than a
request: there is no server there, the name is wrong, the password was
refused, the connection string will not parse. Every one of those arrives as
an exception of asyncpg's, which nothing above ``datastore`` may name
(``docs/layout.md``) and which would otherwise reach ``robinauts start`` and
``robinauts db init`` as a traceback about a library.

So ``datastore.open_pool`` turns them into ``domain.DatabaseUnreachableError``.
Two things are tested here and they are of a piece: that it really does, for
each family of failure, and that what comes out **cannot carry a password**.
The second is the one that matters: a failure to connect is exactly the moment
somebody copies a message into a ticket.

No database is needed. The driver is stood in for, because what is being
tested is the translation and not PostgreSQL.
"""

from __future__ import annotations

from typing import Any

import asyncpg
import pytest

from aio import asyncio_test
from robinauts.legacy.datastore import open_pool
from robinauts.legacy.datastore.pool import OPENING_FAILURES
from robinauts.legacy.domain import REDACTED, DatabaseUnreachableError, without_secrets

DSN = "postgresql://robinauts:hunter2@db.example.com:5432/robinauts"
"""A connection string with a password in it, as a deployment's really is."""

PASSWORD = "hunter2"


def refusals() -> list[BaseException]:
    """One failure of each family ``OPENING_FAILURES`` names."""
    return [
        asyncpg.InvalidCatalogNameError('database "robinauts" does not exist'),
        asyncpg.InvalidPasswordError('password authentication failed for user "robinauts"'),
        asyncpg.exceptions.ClientConfigurationError(f"invalid DSN: {DSN}"),
        ConnectionRefusedError(111, "Connect call failed ('127.0.0.1', 5432)"),
        OSError("no route to host"),
    ]


@pytest.mark.parametrize("refused", refusals(), ids=lambda one: type(one).__name__)
@asyncio_test
async def test_every_way_a_database_refuses_to_open_becomes_one_of_ours(
    monkeypatch: Any, refused: BaseException
) -> None:
    async def never(*_: Any, **__: Any) -> Any:
        raise refused

    monkeypatch.setattr(asyncpg, "create_pool", never)

    with pytest.raises(DatabaseUnreachableError) as raised:
        await open_pool(DSN)

    # The driver's own sentence is kept -- it is what says which of the five
    # this was -- and the original is chained, so a log with a traceback in it
    # still has the driver's frames.
    assert raised.value.__cause__ is refused
    assert isinstance(raised.value, Exception)


@pytest.mark.parametrize("refused", refusals(), ids=lambda one: type(one).__name__)
def test_nothing_a_driver_said_can_carry_the_connection_string(
    refused: BaseException,
) -> None:
    said = str(DatabaseUnreachableError.from_driver(refused))

    assert PASSWORD not in said
    assert DSN not in said
    assert "://" not in said


def test_the_families_are_the_ones_the_driver_really_raises() -> None:
    """Each class in ``refusals`` is under one of the three ``open_pool`` catches.

    Written out because the catch is broad on purpose: a code nobody thought
    of is still an operator's problem, and this is the check that the three
    families really do cover what asyncpg raises.
    """
    assert all(isinstance(one, OPENING_FAILURES) for one in refusals())


@pytest.mark.parametrize(
    ("said", "left"),
    [
        ('database "robinauts" does not exist', 'database "robinauts" does not exist'),
        (f"invalid DSN: {DSN}", f"invalid DSN: {REDACTED}"),
        (
            f"could not connect to {DSN} after 3 tries",
            f"could not connect to {REDACTED} after 3 tries",
        ),
        ("password=hunter2 is wrong", f"{REDACTED} is wrong"),
        ("PGPASSWORD: hunter2", f"{REDACTED}"),
        ("passfile=/etc/robinauts/.pgpass", f"{REDACTED}"),
        # A driver quoting the arguments it was handed, the way its own
        # language spells a mapping. The name may be quoted, the value may be
        # quoted, and a quoted value runs to its closing quote -- a password
        # with a space in it is still a password.
        ('{"user": "robinauts", "password": "hunter2"}', '{"user": "robinauts", <redacted>}'),
        ("{'password': 'hunter 2'}", "{<redacted>}"),
        ('password = "hunter 2" was refused', f"{REDACTED} was refused"),
        # Two of them in one sentence, and both go.
        (f"{DSN} and postgres://a:b@c/d", f"{REDACTED} and {REDACTED}"),
        # Nothing to take out is nothing taken out.
        ("too many connections for role", "too many connections for role"),
    ],
)
def test_what_is_taken_out_of_what_a_driver_said(said: str, left: str) -> None:
    assert without_secrets(said) == left


def test_a_driver_that_said_nothing_at_all_still_says_which_it_was() -> None:
    # `str(exc)` of an exception raised with no message is the empty string,
    # and "the database could not be opened: " helps nobody.
    said = str(DatabaseUnreachableError.from_driver(ConnectionResetError()))

    assert "ConnectionResetError" in said
