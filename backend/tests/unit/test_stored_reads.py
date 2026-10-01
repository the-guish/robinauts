# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Reading our own rows: what a store wraps, and what it is answered with."""

import uuid
from datetime import datetime

import pytest

from conversations import AGENT, CONVERSATION, MODEL, OWNER, RUN, answer, at, question
from robinauts.legacy.core import message_from_stored, message_to_data
from robinauts.legacy.domain import (
    Conversation,
    InvalidValueError,
    MessageNotFoundError,
    Run,
    RunState,
    StoredDataError,
    reading_stored,
)

ROW = {
    "id": CONVERSATION,
    "owner_id": OWNER,
    "agent": AGENT,
    "model": MODEL,
    "created_at": at(0),
    "updated_at": at(0),
    "title": "What is a robinaut?",
}
"""A conversation's row: columns a store owns, and builds a record from."""


def conversation_from_row(row: dict[str, object]) -> Conversation:
    """What `datastore` will do, written here so that the wrapping is tested.

    A store builds the **flat** records whose columns it owns, inside
    ``reading_stored``: each of them validates itself, and an
    ``InvalidValueError`` escaping a store would be answered as though the
    request had been at fault. It does not build a message -- that crosses
    the port as the document ``core`` wrote (see below).
    """
    with reading_stored("a stored conversation cannot be read by this build"):
        return Conversation(
            id=row["id"],
            owner_id=row["owner_id"],
            agent=row["agent"],
            model=row["model"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            title=row["title"],
        )


def test_a_row_that_is_what_it_should_be_is_read() -> None:
    assert conversation_from_row(ROW).agent == AGENT


@pytest.mark.parametrize(
    "broken",
    [
        {"created_at": datetime(2026, 9, 21, 9, 0)},
        {"id": "not a uuid"},
        {"owner_id": None},
        {"agent": "An Agent"},
        {"model": None},
        {"title": "two\nlines"},
        {},
    ],
)
def test_a_row_this_build_cannot_read_is_a_fault_of_ours(broken: dict[str, object]) -> None:
    row = {**ROW, **broken} if broken else {key: ROW[key] for key in list(ROW)[:-1]}
    with pytest.raises(StoredDataError) as refused:
        conversation_from_row(row)
    assert refused.value.__cause__ is not None
    assert "cannot be read" in str(refused.value)


def test_a_message_crosses_the_port_as_the_document_core_wrote() -> None:
    """A store keeps it as it is and hands it back; the application is what
    decodes it, because the format is core's and a store may not import it."""
    said = answer(question(), "Some one")
    kept = message_to_data(said)
    assert message_from_stored(kept) == said
    with pytest.raises(StoredDataError):
        message_from_stored({**kept, "role": "nonsense"})


def test_the_same_wrapping_serves_every_flat_record() -> None:
    for build in (
        lambda: Conversation(
            id=CONVERSATION,
            owner_id=OWNER,
            agent="An Agent",
            model=MODEL,
            created_at=at(0),
            updated_at=at(0),
        ),
        lambda: Run(
            id=RUN,
            conversation_id=CONVERSATION,
            message_id=uuid.uuid4(),
            agent=AGENT,
            engine="pydantic-ai",
            model="sonnet",
            state=RunState.RUNNING,
            created_at=at(0),
        ),
    ):
        with pytest.raises(StoredDataError):
            with reading_stored("a stored row cannot be read by this build"):
                build()


def test_an_id_a_request_named_passes_straight_through() -> None:
    """Nothing is wrong with the rows, so this says nothing about them. It is
    not an ``InvalidValueError`` and not one of the four shapes above, so it
    is none of the reader's business."""
    with pytest.raises(MessageNotFoundError):
        with reading_stored("a stored message cannot be read by this build"):
            raise MessageNotFoundError("no message here")


@pytest.mark.parametrize(
    "broken",
    [
        KeyError("parts"),
        TypeError("not a mapping"),
        AttributeError("no such field"),
        ValueError("could not parse a column"),
    ],
)
def test_a_row_that_cannot_even_be_taken_apart_is_stored_data_too(broken: Exception) -> None:
    """A column that is not there, a value that is not what it should be: the
    store is the last place that can say the row was the trouble."""
    with pytest.raises(StoredDataError) as refused:
        with reading_stored("a stored message cannot be read by this build"):
            raise broken
    assert refused.value.__cause__ is broken


def test_what_is_not_about_reading_a_row_passes_through() -> None:
    with pytest.raises(RuntimeError):
        with reading_stored("a stored message cannot be read by this build"):
            raise RuntimeError("the pool is closed")
    with pytest.raises(InvalidValueError):
        raise InvalidValueError("outside the reader, this is what a request meets")


def test_the_driver_keeps_its_own_errors() -> None:
    """A connection that dropped is not a row that cannot be read.

    ``asyncpg`` raises nothing that this reader converts -- with one exception
    which is raised where a pool is configured and never inside one of these
    blocks -- so a store's own failures stay its own and are retried as what
    they are.
    """
    asyncpg = pytest.importorskip("asyncpg")
    from asyncpg import exceptions

    converted = (KeyError, TypeError, AttributeError, ValueError, RecursionError)
    query_errors = (
        exceptions.PostgresError,
        exceptions.InterfaceError,
        exceptions.InternalClientError,
    )
    caught = {
        name
        for name, kind in vars(exceptions).items()
        if isinstance(kind, type) and issubclass(kind, query_errors) and issubclass(kind, converted)
    }
    assert caught == {"ClientConfigurationError"}, caught
    # And that one is about a pool's configuration, not about a row.
    assert issubclass(exceptions.ClientConfigurationError, exceptions.InterfaceError)
    assert not issubclass(asyncpg.PostgresError, converted)


def test_a_cancelled_request_is_not_told_its_rows_are_broken() -> None:
    """``CancelledError`` is a ``BaseException``: it is never caught here."""
    import asyncio

    with pytest.raises(asyncio.CancelledError):
        with reading_stored("a stored message cannot be read by this build"):
            raise asyncio.CancelledError()
