# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Managing conversations, over the fakes: ownership, listing, opening, deleting.

The service is ``application.Conversations`` and every port under it is a
fake, so what is tested here is the control flow and the rules it applies --
not a store. What a store must do is ``tests/contracts/``.

The one thing said in more ways than one is **ownership**: it is checked on
every operation, and a conversation of somebody else's has to be
indistinguishable from one that was never there. The table at the bottom runs
every operation twice, once with each, and requires the two to answer the
same.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import pytest

from aio import asyncio_test
from conversations import (
    CONVERSATION,
    OTHER_CONVERSATION,
    OWNER,
    RUN,
    answer,
    at,
    conversation,
    question,
    run,
)
from fakes import FakeClock, MemoryConversationStore
from robinauts.application import Conversations
from robinauts.core import message_to_data, run_event_to_data, transition
from robinauts.domain import (
    FIRST_POSITION,
    Conversation,
    ConversationNotFoundError,
    InvalidValueError,
    Message,
    MessageCompleted,
    MessageStarted,
    RunAlreadyActiveError,
    RunEnded,
    RunEvent,
    RunStarted,
    RunState,
    StoredDataError,
    TextPiece,
    TurnEvent,
    User,
)

NOW = at(100)
"""What the clock says while a test is running, unless it moves it."""

STRANGER = uuid.UUID("77777777-7777-4777-8777-777777777777")
"""Somebody else's user id; nothing of theirs is here and nothing of ours is theirs."""

AUTHOR = User(id=OWNER, provider="google", subject="1", name="Ada", email=None, created_at=at(0))
SOMEBODY_ELSE = User(
    id=STRANGER, provider="google", subject="2", name="Bob", email=None, created_at=at(0)
)


@dataclass(frozen=True, slots=True)
class Wired:
    """The service with its store behind it, and the store a test looks in."""

    service: Conversations
    store: MemoryConversationStore
    clock: FakeClock

    async def written(self, *messages: Message, **changes: object) -> Conversation:
        """A conversation of the author's, with those messages appended in order."""
        kept = conversation(**changes)
        await self.store.add_conversation(kept)
        for message in messages:
            await self.store.append_message(
                message, message_to_data(message), now=message.created_at
            )
        return kept

    async def answering(self, asked: Message, *events: TurnEvent) -> None:
        """A run in flight answering ``asked``, with those events numbered from 1."""
        await self.store.start_run(
            conversation=None, message=None, run=run(message_id=asked.id), now=at(9)
        )
        for event in events:
            position = await self.store.last_position(RUN) + 1
            kept = RunEvent(run_id=RUN, seq=position, event=event)
            await self.store.append_event(kept, run_event_to_data(kept))


def wired() -> Wired:
    """The service, its store and a clock that stands still."""
    store = MemoryConversationStore()
    clock = FakeClock(now=NOW)
    return Wired(service=Conversations(store=store, clock=clock), store=store, clock=clock)


# --- listing -----------------------------------------------------------------


@asyncio_test
async def test_a_listing_holds_this_persons_conversations_newest_updated_first() -> None:
    wiring = wired()
    for seconds in (0, 20, 10):
        await wiring.store.add_conversation(
            conversation(id=uuid.uuid4(), updated_at=at(seconds), title=f"at {seconds}")
        )
    await wiring.store.add_conversation(
        conversation(id=OTHER_CONVERSATION, owner_id=STRANGER, title="not mine")
    )

    page = await wiring.service.list_for(AUTHOR, limit=10)

    assert [kept.title for kept in page.conversations] == ["at 20", "at 10", "at 0"]
    assert page.cursor is None


@asyncio_test
async def test_a_listing_pages_with_the_cursor_it_handed_back() -> None:
    wiring = wired()
    for seconds in range(5):
        await wiring.store.add_conversation(
            conversation(id=uuid.uuid4(), updated_at=at(seconds), title=f"at {seconds}")
        )

    first = await wiring.service.list_for(AUTHOR, limit=2)
    second = await wiring.service.list_for(AUTHOR, limit=2, cursor=first.cursor)

    assert [kept.title for kept in first.conversations] == ["at 4", "at 3"]
    assert [kept.title for kept in second.conversations] == ["at 2", "at 1"]


@asyncio_test
async def test_a_page_of_no_size_is_refused_before_a_store_is_asked() -> None:
    wiring = wired()

    for limit in (0, -1, 1_000, "ten", True):
        with pytest.raises(InvalidValueError):
            await wiring.service.list_for(AUTHOR, limit=limit)  # type: ignore[arg-type]


# --- opening -----------------------------------------------------------------


@asyncio_test
async def test_an_empty_conversation_opens_on_nothing() -> None:
    wiring = wired()
    await wiring.written()

    opened = await wiring.service.open(AUTHOR, CONVERSATION)

    assert opened.messages == ()
    assert (opened.run_id, opened.resume) == (None, None)


@asyncio_test
async def test_a_conversation_opens_on_the_path_to_its_newest_message_and_nothing_else() -> None:
    # Two roots -- the first question was edited -- and two answers to the
    # second question. What opens is the one thread a reader sees: the newest
    # root's path down to the newest answer. The edited-away question and the
    # regenerated-away answer are in the store and nowhere in what is opened.
    wiring = wired()
    first = question("First, edited", seconds=0)
    again = question("First, again", seconds=1)
    early = answer(again, "The early answer", seconds=2)
    second = question("And then?", parent=early, seconds=3)
    one = answer(second, "One answer", seconds=4)
    other = answer(second, "The newer answer", seconds=5)
    await wiring.written(first, again, early, second, one, other)

    opened = await wiring.service.open(AUTHOR, CONVERSATION)

    assert len(await wiring.store.messages_of(CONVERSATION)) == 6
    assert [kept.id for kept in opened.messages] == [
        again.id,
        early.id,
        second.id,
        other.id,
    ]


@asyncio_test
async def test_opening_a_conversation_with_a_run_in_flight_says_where_to_attach() -> None:
    wiring = wired()
    asked = question("What is a robinaut?", seconds=0)
    done = answer(asked, "Someone who plays fair.", seconds=2)
    await wiring.written(asked, done)
    still_going = uuid.uuid4()
    await wiring.answering(
        asked,
        RunStarted(run_id=RUN, conversation_id=CONVERSATION),
        MessageStarted(run_id=RUN, message_id=done.id, parent_id=asked.id),
        TextPiece(run_id=RUN, message_id=done.id, text="Someone "),
        MessageCompleted(run_id=RUN, message=done),
        MessageStarted(run_id=RUN, message_id=still_going, parent_id=done.id),
    )

    opened = await wiring.service.open(AUTHOR, CONVERSATION)

    assert opened.run_id == RUN
    assert opened.resume is not None
    # After the last message the run completed, and the next one announced
    # will hang under it: nothing is replayed that is already in the tree.
    assert opened.resume.after == FIRST_POSITION + 3
    assert opened.resume.follows == done.id


@asyncio_test
async def test_opening_during_a_regeneration_shows_the_thread_up_to_its_question() -> None:
    # Q1 A1 Q2 A2, and A1 is being regenerated. The run has written nothing
    # yet, so A2 is still the newest leaf -- but A1, Q2 and A2 are what the
    # run is putting aside, and the new answer will arrive under Q1.
    wiring = wired()
    first = question("One?", seconds=0)
    said = answer(first, "1", seconds=1)
    second = question("Two?", parent=said, seconds=2)
    replied = answer(second, "2", seconds=3)
    await wiring.written(first, said, second, replied)
    coming = uuid.uuid4()
    await wiring.answering(
        first,
        RunStarted(run_id=RUN, conversation_id=CONVERSATION),
        MessageStarted(run_id=RUN, message_id=coming, parent_id=first.id),
        TextPiece(run_id=RUN, message_id=coming, text="1, "),
    )

    opened = await wiring.service.open(AUTHOR, CONVERSATION)

    assert [kept.id for kept in opened.messages] == [first.id]
    assert opened.resume is not None
    assert opened.resume.follows == first.id


@asyncio_test
async def test_opening_during_a_regeneration_that_has_completed_an_answer_shows_it() -> None:
    wiring = wired()
    first = question("One?", seconds=0)
    said = answer(first, "1", seconds=1)
    second = question("Two?", parent=said, seconds=2)
    replied = answer(second, "2", seconds=3)
    again = answer(first, "1, again", seconds=10)
    await wiring.written(first, said, second, replied, again)
    await wiring.answering(
        first,
        RunStarted(run_id=RUN, conversation_id=CONVERSATION),
        MessageStarted(run_id=RUN, message_id=again.id, parent_id=first.id),
        MessageCompleted(run_id=RUN, message=again),
    )

    opened = await wiring.service.open(AUTHOR, CONVERSATION)

    assert [kept.id for kept in opened.messages] == [first.id, again.id]


@asyncio_test
async def test_opening_during_an_ordinary_turn_shows_the_question_being_answered() -> None:
    wiring = wired()
    first = question("One?", seconds=0)
    said = answer(first, "1", seconds=1)
    second = question("Two?", parent=said, seconds=2)
    await wiring.written(first, said, second)
    await wiring.answering(second, RunStarted(run_id=RUN, conversation_id=CONVERSATION))

    opened = await wiring.service.open(AUTHOR, CONVERSATION)

    assert [kept.id for kept in opened.messages] == [first.id, said.id, second.id]
    assert opened.resume is not None
    assert opened.resume.follows == second.id


@asyncio_test
async def test_a_run_whose_completed_message_is_not_stored_is_a_fault_of_ours() -> None:
    # A store writes a message and its announcement together; a snapshot in
    # which one is missing is our rows, not a 404 for whoever opened it.
    wiring = wired()
    asked = question(seconds=0)
    await wiring.written(asked)
    await wiring.answering(
        asked,
        RunStarted(run_id=RUN, conversation_id=CONVERSATION),
        MessageCompleted(run_id=RUN, message=answer(asked, "never stored", seconds=1)),
    )

    with pytest.raises(StoredDataError):
        await wiring.service.open(AUTHOR, CONVERSATION)


@asyncio_test
async def test_a_run_that_has_ended_is_not_a_run_to_attach_to() -> None:
    wiring = wired()
    asked = question(seconds=0)
    await wiring.written(asked)
    await wiring.answering(asked, RunStarted(run_id=RUN, conversation_id=CONVERSATION))
    over = RunEvent(
        run_id=RUN,
        seq=FIRST_POSITION + 1,
        event=RunEnded(run_id=RUN, state=RunState.FINISHED, error=None),
    )
    going = await wiring.store.run_by_id(RUN)
    assert going is not None
    await wiring.store.end_run(
        transition(going, RunState.FINISHED, now=at(10)), over, run_event_to_data(over)
    )

    opened = await wiring.service.open(AUTHOR, CONVERSATION)

    assert (opened.run_id, opened.resume) == (None, None)
    # It finished; there is nothing to be told about.
    assert opened.ended_badly is None


@asyncio_test
async def test_a_conversation_whose_last_run_went_wrong_says_so_when_it_is_opened() -> None:
    # Otherwise somebody who reloads after a failure finds a turn that simply
    # stops, with nothing anywhere saying why (``docs/specs/runs.md``).
    wiring = wired()
    asked = question(seconds=0)
    await wiring.written(asked)
    await wiring.answering(asked, RunStarted(run_id=RUN, conversation_id=CONVERSATION))
    over = RunEvent(
        run_id=RUN,
        seq=FIRST_POSITION + 1,
        event=RunEnded(run_id=RUN, state=RunState.FAILED, error="the provider said no"),
    )
    going = await wiring.store.run_by_id(RUN)
    assert going is not None
    await wiring.store.end_run(
        transition(going, RunState.FAILED, now=at(10), error="the provider said no"),
        over,
        run_event_to_data(over),
    )

    opened = await wiring.service.open(AUTHOR, CONVERSATION)

    assert opened.run_id is None
    assert opened.ended_badly is not None
    assert opened.ended_badly.state is RunState.FAILED
    assert opened.ended_badly.error == "the provider said no"


@asyncio_test
async def test_a_conversation_with_a_run_in_flight_is_told_about_the_run_and_nothing_else() -> None:
    wiring = wired()
    asked = question(seconds=0)
    await wiring.written(asked)
    await wiring.answering(asked, RunStarted(run_id=RUN, conversation_id=CONVERSATION))

    opened = await wiring.service.open(AUTHOR, CONVERSATION)

    # What matters while one is going is the run that is going.
    assert opened.run_id == RUN
    assert opened.ended_badly is None


# --- renaming ----------------------------------------------------------------


@asyncio_test
async def test_renaming_gives_the_new_title_and_dates_the_conversation() -> None:
    wiring = wired()
    await wiring.written()

    renamed = await wiring.service.rename(AUTHOR, CONVERSATION, "What I called it")

    assert (renamed.title, renamed.updated_at) == ("What I called it", NOW)
    assert await wiring.store.conversation_by_id(CONVERSATION) == renamed


@asyncio_test
async def test_a_title_that_is_not_one_line_of_text_is_refused() -> None:
    wiring = wired()
    await wiring.written()

    for title in ("two\nlines", "a tab\there", "\x00", "x" * 121, 7):
        with pytest.raises(InvalidValueError):
            await wiring.service.rename(AUTHOR, CONVERSATION, title)  # type: ignore[arg-type]

    found = await wiring.store.conversation_by_id(CONVERSATION)
    assert found is not None and found.title == "What is a robinaut?"


# --- deleting ----------------------------------------------------------------


@asyncio_test
async def test_deleting_takes_the_messages_the_runs_and_their_events_with_it() -> None:
    wiring = wired()
    asked = question("What is deleted?", seconds=0)
    await wiring.written(asked)
    await wiring.answering(asked, RunStarted(run_id=RUN, conversation_id=CONVERSATION))
    going = await wiring.store.run_by_id(RUN)
    assert going is not None
    over = RunEvent(
        run_id=RUN,
        seq=FIRST_POSITION + 1,
        event=RunEnded(run_id=RUN, state=RunState.FINISHED, error=None),
    )
    await wiring.store.end_run(
        transition(going, RunState.FINISHED, now=at(10)), over, run_event_to_data(over)
    )
    kept = conversation(id=OTHER_CONVERSATION, title="What is kept?")
    await wiring.store.add_conversation(kept)

    await wiring.service.delete(AUTHOR, CONVERSATION)

    assert await wiring.store.conversation_by_id(CONVERSATION) is None
    held = wiring.store.everything()
    assert str(CONVERSATION) not in held
    assert str(RUN) not in held
    assert await wiring.store.conversation_by_id(OTHER_CONVERSATION) == kept


@asyncio_test
async def test_deleting_is_refused_while_a_run_is_going_and_nothing_goes() -> None:
    wiring = wired()
    asked = question(seconds=0)
    await wiring.written(asked)
    await wiring.answering(asked, RunStarted(run_id=RUN, conversation_id=CONVERSATION))

    with pytest.raises(RunAlreadyActiveError):
        await wiring.service.delete(AUTHOR, CONVERSATION)

    assert await wiring.store.conversation_by_id(CONVERSATION) is not None
    assert await wiring.store.run_by_id(RUN) is not None
    assert len(await wiring.store.messages_of(CONVERSATION)) == 1


class DeletingUnderneath(MemoryConversationStore):
    """A store that loses the conversation between the two calls `delete` makes.

    The ownership check reads it and it is there; by the time the delete runs
    somebody else -- another tab, another request -- has already deleted it.
    The path cannot be reached by deleting first, because then the ownership
    check is what fails; it needs the row to go in between.
    """

    async def conversation_by_id(self, conversation_id: uuid.UUID) -> Conversation | None:
        found = await super().conversation_by_id(conversation_id)
        await super().delete_conversation(conversation_id, now=NOW)
        return found


@asyncio_test
async def test_deleting_one_that_goes_between_the_two_calls_is_not_an_error() -> None:
    # What the caller asked for is what is true, so there is nothing to
    # report: `delete` answers by returning.
    store = DeletingUnderneath()
    service = Conversations(store=store, clock=FakeClock(now=NOW))
    await store.add_conversation(conversation())

    await service.delete(AUTHOR, CONVERSATION)

    assert await MemoryConversationStore.conversation_by_id(store, CONVERSATION) is None


@asyncio_test
async def test_deleting_one_that_was_already_gone_is_answered_as_not_there() -> None:
    wiring = wired()
    await wiring.written()
    await wiring.store.delete_conversation(CONVERSATION, now=NOW)

    with pytest.raises(ConversationNotFoundError):
        await wiring.service.delete(AUTHOR, CONVERSATION)


# --- ownership ---------------------------------------------------------------

Operation = Callable[[Conversations, User, uuid.UUID], Awaitable[object]]

OPERATIONS: dict[str, Operation] = {
    "open": lambda service, user, which: service.open(user, which),
    "rename": lambda service, user, which: service.rename(user, which, "Renamed"),
    "delete": lambda service, user, which: service.delete(user, which),
}
"""Every operation that names a conversation. Listing names none: it asks for
the caller's own, and a person with none is not a person refused."""


@pytest.mark.parametrize("name", sorted(OPERATIONS))
@asyncio_test
async def test_somebody_elses_conversation_answers_as_one_that_is_not_there(name: str) -> None:
    operation = OPERATIONS[name]
    wiring = wired()
    asked = question(seconds=0)
    await wiring.written(asked)
    before = wiring.store.everything()

    with pytest.raises(ConversationNotFoundError) as theirs:
        await operation(wiring.service, SOMEBODY_ELSE, CONVERSATION)
    with pytest.raises(ConversationNotFoundError) as nowhere:
        await operation(wiring.service, AUTHOR, uuid.uuid4())

    # The same error, from the same place, and nothing written either way.
    # The words differ and never leave the process: `api` answers the whole
    # family one fixed body.
    assert type(theirs.value) is type(nowhere.value)
    assert wiring.store.everything() == before


@asyncio_test
async def test_the_owner_of_a_conversation_is_read_from_the_store_every_time() -> None:
    # Nothing is cached: a conversation handed to somebody else -- which
    # nothing does yet -- would be theirs from the next call, not the next
    # restart.
    wiring = wired()
    await wiring.written(owner_id=STRANGER)

    with pytest.raises(ConversationNotFoundError):
        await wiring.service.open(AUTHOR, CONVERSATION)

    assert (await wiring.service.open(SOMEBODY_ELSE, CONVERSATION)).conversation.owner_id == (
        STRANGER
    )


@asyncio_test
async def test_a_conversation_named_by_something_that_is_no_id_is_refused() -> None:
    wiring = wired()
    await wiring.written()

    with pytest.raises(InvalidValueError):
        await wiring.service.open(AUTHOR, str(CONVERSATION))  # type: ignore[arg-type]
