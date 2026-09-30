# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What every ``ConversationStore`` must do with runs and their events.

The second half of **one** suite over **one** store: this class extends
``ConversationStoreContract`` and uses its ``new_store`` / ``close_store`` /
``dump`` hooks, so a store is certified by a single class that inherits both.
The split is about the length of a module and nothing else.

Four promises of the port are the reason this half exists, and none of them
survives a store built out of a read and a later write:

- **at most one active run per conversation**. ``start_run`` refuses a second
  in the step that would have inserted it, so two requests arriving together
  leave one run and one refusal rather than two answers writing into one
  conversation.
- **a turn begins all at once**. The conversation, its first question and the
  run are one transaction: nothing is half-created and no refusal leaves a
  conversation behind.
- **a position is stored once**. Of several callers offering one position at
  once exactly one succeeds and every other is told the position is taken.
- **nothing is announced that is not stored, and nothing stored goes
  unannounced**. A message and its ``MessageCompleted``, a run's end and its
  ``RunEnded``: both or neither.

What this asks of an implementation: a pool rather than one shared
connection; the look-and-insert of ``start_run`` indivisible (a unique index
over "the active run of a conversation", or a lock keyed on the conversation);
the position a primary key, so that a duplicate is refused by the database and
not by a count another transaction has not committed to yet; and a real
transaction around each of the compound methods.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable
from dataclasses import replace

import pytest

from aio import asyncio_test
from contracts.conversation_store import ConversationStoreContract
from conversations import (
    CONVERSATION,
    OTHER_CONVERSATION,
    RUN,
    answer,
    at,
    conversation,
    ended,
    provenance,
    question,
    run,
)
from robinauts.core import (
    check_event_order,
    message_from_data,
    message_to_data,
    run_event_from_stored,
    run_event_to_data,
    transition,
)
from robinauts.domain import (
    FIRST_POSITION,
    ConversationNotFoundError,
    IllegalTransitionError,
    InvalidValueError,
    Message,
    MessageCompleted,
    MessageNotFoundError,
    MessageStarted,
    NotFoundError,
    PositionTakenError,
    Role,
    Run,
    RunAlreadyActiveError,
    RunEnded,
    RunEvent,
    RunNotFoundError,
    RunStarted,
    RunState,
    TextPart,
    TextPiece,
    ToolCallPart,
    ToolResultPart,
)
from robinauts.ports import MAX_PAGE, MAX_SWEPT, ConversationStore, Document, Snapshot

OTHER_RUN = uuid.UUID("66666666-6666-4666-8666-666666666666")
"""A second run, for the conversation beside the one under test."""


def started(run_id: uuid.UUID, *, conversation_id: uuid.UUID = CONVERSATION) -> RunEvent:
    """The first event of a run, at ``FIRST_POSITION``."""
    return RunEvent(
        run_id=run_id,
        seq=FIRST_POSITION,
        event=RunStarted(run_id=run_id, conversation_id=conversation_id),
    )


def delta(run_id: uuid.UUID, seq: int, text: str = "more") -> RunEvent:
    """A piece of a message being produced, at ``seq``."""
    return RunEvent(
        run_id=run_id, seq=seq, event=TextPiece(run_id=run_id, message_id=OTHER_RUN, text=text)
    )


def completed(run_id: uuid.UUID, seq: int, message: Message) -> RunEvent:
    """The announcement that a message is complete and stored."""
    return RunEvent(run_id=run_id, seq=seq, event=MessageCompleted(run_id=run_id, message=message))


def announced(run_id: uuid.UUID, seq: int, message: Message) -> RunEvent:
    """The announcement that a message is being produced under that id."""
    assert message.parent_id is not None
    return RunEvent(
        run_id=run_id,
        seq=seq,
        event=MessageStarted(
            run_id=run_id,
            message_id=message.id,
            parent_id=message.parent_id,
            role=message.role,
        ),
    )


def over(run_id: uuid.UUID, seq: int, state: RunState) -> RunEvent:
    """The announcement that a run has ended."""
    return RunEvent(
        run_id=run_id,
        seq=seq,
        event=RunEnded(run_id=run_id, state=state, error=None),
    )


class ConversationRunsContract(ConversationStoreContract):
    """The runs and events half of the contract. Inherits the first half."""

    # Starting a turn.

    @asyncio_test
    async def test_a_new_chat_creates_the_conversation_its_question_and_its_run(self) -> None:
        async with self.opened() as store:
            kept = conversation(title="")
            asked = question(seconds=0)

            await store.start_run(
                conversation=kept,
                message=(asked, message_to_data(asked)),
                run=run(message_id=asked.id),
                now=at(1),
            )

            found = await store.conversation_by_id(CONVERSATION)
            assert found is not None
            assert found.updated_at == at(1)
            assert [document["id"] for document in await store.messages_of(CONVERSATION)] == [
                str(asked.id)
            ]
            going = await store.active_run_of(CONVERSATION)
            assert going is not None and going.id == RUN

    @asyncio_test
    async def test_a_message_in_a_conversation_that_exists_starts_a_run(self) -> None:
        async with self.opened() as store:
            await store.add_conversation(conversation())
            first = question(seconds=0)
            await store.append_message(first, message_to_data(first), now=at(1))
            replied = answer(first, seconds=2)
            await store.append_message(replied, message_to_data(replied), now=at(2))
            asked = question("And then?", parent=replied, seconds=3)

            await store.start_run(
                conversation=None,
                message=(asked, message_to_data(asked)),
                run=run(message_id=asked.id),
                now=at(3),
            )

            assert len(await store.messages_of(CONVERSATION)) == 3
            assert await store.active_run_of(CONVERSATION) is not None

    @asyncio_test
    async def test_a_regeneration_starts_a_run_and_appends_nothing(self) -> None:
        async with self.opened() as store:
            await store.add_conversation(conversation())
            asked = question(seconds=0)
            await store.append_message(asked, message_to_data(asked), now=at(1))

            await store.start_run(
                conversation=None, message=None, run=run(message_id=asked.id), now=at(5)
            )

            assert len(await store.messages_of(CONVERSATION)) == 1
            found = await store.conversation_by_id(CONVERSATION)
            assert found is not None and found.updated_at == at(5)
            assert await store.active_run_of(CONVERSATION) is not None

    @asyncio_test
    async def test_a_second_run_is_refused_and_leaves_nothing_of_itself(self) -> None:
        async with self.opened() as store:
            kept = conversation()
            asked = question(seconds=0)
            await store.start_run(
                conversation=kept,
                message=(asked, message_to_data(asked)),
                run=run(message_id=asked.id),
                now=at(1),
            )
            again = question("At the same time?", parent=asked, seconds=2)

            with pytest.raises(RunAlreadyActiveError):
                await store.start_run(
                    conversation=None,
                    message=(again, message_to_data(again)),
                    run=run(id=OTHER_RUN, message_id=again.id),
                    now=at(2),
                )

            assert await store.run_by_id(OTHER_RUN) is None
            assert len(await store.messages_of(CONVERSATION)) == 1

    @asyncio_test
    async def test_a_run_in_a_conversation_that_is_not_there_is_refused(self) -> None:
        async with self.opened() as store:
            asked = question(seconds=0)
            held = await self.dump(store)

            with pytest.raises(ConversationNotFoundError):
                await store.start_run(
                    conversation=None,
                    message=(asked, message_to_data(asked)),
                    run=run(message_id=asked.id),
                    now=at(1),
                )

            assert await store.run_by_id(RUN) is None
            assert await self.dump(store) == held

    @asyncio_test
    async def test_a_new_chat_cannot_reuse_a_message_id_of_another_conversation(self) -> None:
        # The conversation being created has no messages to look in, so a
        # store that checked the id against it alone would let one id name two
        # messages of the deployment.
        async with self.opened() as store:
            await _begun(store, run_id=OTHER_RUN, conversation_id=OTHER_CONVERSATION, event=False)
            (taken,) = await store.messages_of(OTHER_CONVERSATION)
            held = await self.dump(store)
            asked = question(id=uuid.UUID(taken["id"]), seconds=0)

            with pytest.raises(InvalidValueError):
                await store.start_run(
                    conversation=conversation(),
                    message=(asked, message_to_data(asked)),
                    run=run(message_id=asked.id),
                    now=at(1),
                )

            assert await store.conversation_by_id(CONVERSATION) is None
            assert await self.dump(store) == held

    @asyncio_test
    async def test_a_question_whose_parent_is_no_message_of_it_is_refused(self) -> None:
        async with self.opened() as store:
            await store.add_conversation(conversation())
            asked = question(parent=uuid.uuid4(), seconds=0)

            with pytest.raises(MessageNotFoundError):
                await store.start_run(
                    conversation=None,
                    message=(asked, message_to_data(asked)),
                    run=run(message_id=asked.id),
                    now=at(1),
                )

            assert await store.messages_of(CONVERSATION) == ()
            assert await store.run_by_id(RUN) is None

    @asyncio_test
    async def test_a_regeneration_of_a_message_that_is_not_there_is_refused(self) -> None:
        async with self.opened() as store:
            await store.add_conversation(conversation())

            with pytest.raises(MessageNotFoundError):
                await store.start_run(
                    conversation=None, message=None, run=run(message_id=uuid.uuid4()), now=at(1)
                )

            assert await store.run_by_id(RUN) is None

    @asyncio_test
    async def test_a_run_answers_a_question_and_never_an_answer(self) -> None:
        async with self.opened() as store:
            asked = await _begun(store)
            replied = answer(asked, seconds=3)
            await _completed(store, replied, FIRST_POSITION + 1)
            await _ended(store, RunState.FINISHED)

            # Regenerating, naming the answer rather than the question.
            with pytest.raises(InvalidValueError):
                await store.start_run(
                    conversation=None,
                    message=None,
                    run=run(id=OTHER_RUN, message_id=replied.id),
                    now=at(9),
                )
            # And a turn whose new message is not a question either.
            another = answer(replied, "not a question", seconds=9)
            with pytest.raises(InvalidValueError):
                await store.start_run(
                    conversation=None,
                    message=(another, message_to_data(another)),
                    run=run(id=OTHER_RUN, message_id=another.id),
                    now=at(9),
                )

            assert await store.run_by_id(OTHER_RUN) is None
            assert len(await store.messages_of(CONVERSATION)) == 2

    @asyncio_test
    async def test_a_run_is_bound_to_its_conversations_agent(self) -> None:
        # A conversation is bound to one agent; a run of another agent in it
        # would make its answers say they came from an agent it is not.
        async with self.opened() as store:
            asked = question(seconds=0)
            held = await self.dump(store)

            with pytest.raises(InvalidValueError):
                await store.start_run(
                    conversation=conversation(agent="helper"),
                    message=(asked, message_to_data(asked)),
                    run=run(message_id=asked.id, agent="other"),
                    now=at(1),
                )

            assert await self.dump(store) == held

    @asyncio_test
    async def test_a_turn_whose_records_do_not_name_each_other_is_refused(self) -> None:
        async with self.opened() as store:
            await store.add_conversation(conversation())
            asked = question(seconds=0)

            with pytest.raises(InvalidValueError):
                await store.start_run(
                    conversation=None,
                    message=(asked, message_to_data(asked)),
                    run=run(message_id=uuid.uuid4()),
                    now=at(1),
                )

            assert await store.messages_of(CONVERSATION) == ()

    @asyncio_test
    async def test_a_run_begins_active_and_never_in_a_state_it_has_ended_in(self) -> None:
        # A run stored ended with no events under it could never be made
        # whole: nothing would ever end it and no watcher could be told
        # anything about it.
        async with self.opened() as store:
            asked = question(seconds=0)
            held = await self.dump(store)

            with pytest.raises(InvalidValueError):
                await store.start_run(
                    conversation=conversation(),
                    message=(asked, message_to_data(asked)),
                    run=ended(RunState.FINISHED, message_id=asked.id),
                    now=at(1),
                )

            assert await self.dump(store) == held

    @asyncio_test
    async def test_a_run_id_already_stored_is_refused(self) -> None:
        # A store over SQL would meet this as a unique violation on the
        # primary key; it is a refusal of the port either way.
        async with self.opened() as store:
            asked = await _begun(store)
            await _ended(store, RunState.FINISHED)

            with pytest.raises(InvalidValueError):
                await store.start_run(
                    conversation=None,
                    message=None,
                    run=run(message_id=asked.id, created_at=at(9)),
                    now=at(9),
                )

            assert [kept.id for kept in await store.runs_of(CONVERSATION)] == [RUN]
            assert await store.active_run_of(CONVERSATION) is None

    @asyncio_test
    async def test_a_conversation_already_stored_is_not_created_again_by_a_turn(self) -> None:
        async with self.opened() as store:
            await store.add_conversation(conversation())
            asked = question(seconds=0)

            with pytest.raises(InvalidValueError):
                await store.start_run(
                    conversation=conversation(),
                    message=(asked, message_to_data(asked)),
                    run=run(message_id=asked.id),
                    now=at(1),
                )

            assert await store.run_by_id(RUN) is None

    # Reading runs.

    @asyncio_test
    async def test_a_run_nobody_stored_is_not_there(self) -> None:
        async with self.opened() as store:
            assert await store.run_by_id(OTHER_RUN) is None
            assert await store.active_run_of(CONVERSATION) is None
            assert await store.runs_of(CONVERSATION) == ()
            assert await store.events_of(OTHER_RUN) == ()
            assert await store.last_position(OTHER_RUN) == 0

    @asyncio_test
    async def test_a_conversations_runs_come_back_most_recent_first(self) -> None:
        async with self.opened() as store:
            asked = await _begun(store, created_at=at(0))
            await _ended(store, RunState.FINISHED)
            await store.start_run(
                conversation=None,
                message=None,
                run=run(id=OTHER_RUN, message_id=asked.id, created_at=at(5)),
                now=at(5),
            )

            assert [kept.id for kept in await store.runs_of(CONVERSATION)] == [OTHER_RUN, RUN]

    @asyncio_test
    async def test_a_caller_that_wants_the_most_recent_run_asks_for_one(self) -> None:
        # Opening a conversation wants the last run and nothing else; a
        # conversation answered a thousand times must not be read a thousand
        # rows at a time to look at one of them.
        async with self.opened() as store:
            asked = await _begun(store, created_at=at(0))
            await _ended(store, RunState.FINISHED)
            await store.start_run(
                conversation=None,
                message=None,
                run=run(id=OTHER_RUN, message_id=asked.id, created_at=at(5)),
                now=at(5),
            )

            assert [kept.id for kept in await store.runs_of(CONVERSATION, limit=1)] == [OTHER_RUN]
            with pytest.raises(InvalidValueError):
                await store.runs_of(CONVERSATION, limit=0)
            with pytest.raises(InvalidValueError):
                await store.runs_of(CONVERSATION, limit=MAX_PAGE + 1)

    @asyncio_test
    async def test_the_runs_in_a_state_are_what_a_start_up_sweep_asks_for(self) -> None:
        async with self.opened() as store:
            await _begun(store)
            await _begun(store, run_id=OTHER_RUN, conversation_id=OTHER_CONVERSATION, event=False)
            await _ended(store, RunState.FINISHED, run_id=OTHER_RUN)

            going = await store.runs_in({RunState.RUNNING})

            assert [kept.id for kept in going] == [RUN]
            assert {kept.id for kept in await store.runs_in(set(RunState))} == {RUN, OTHER_RUN}
            assert await store.runs_in({RunState.WAITING}) == ()
            # Bounded: never an unbounded read of a table that only grows.
            assert len(await store.runs_in(set(RunState), limit=1)) == 1
            for limit in (0, -1, MAX_SWEPT + 1):
                with pytest.raises(InvalidValueError):
                    await store.runs_in(set(RunState), limit=limit)

    # Changing a run.

    @asyncio_test
    async def test_a_run_still_going_may_change_its_state_and_its_times(self) -> None:
        async with self.opened() as store:
            await _begun(store)

            await store.update_run(transition(await _stored(store), RunState.WAITING, now=at(4)))

            found = await store.run_by_id(RUN)
            assert found is not None and found.state is RunState.WAITING
            assert await store.active_run_of(CONVERSATION) == found

    @asyncio_test
    async def test_a_run_does_not_end_through_update_run(self) -> None:
        async with self.opened() as store:
            await _begun(store)

            with pytest.raises(InvalidValueError):
                await store.update_run(
                    transition(await _stored(store), RunState.FINISHED, now=at(4))
                )

            found = await store.run_by_id(RUN)
            assert found is not None and found.state is RunState.RUNNING

    @asyncio_test
    async def test_a_run_changes_its_state_its_start_and_its_error_and_nothing_else(self) -> None:
        # Its end is `end_run`'s to write, and the rest is what the run is.
        async with self.opened() as store:
            await _begun(store)

            kept = await _stored(store)
            for changed in (
                replace(kept, conversation_id=OTHER_CONVERSATION),
                replace(kept, message_id=uuid.uuid4()),
                replace(kept, model="opus"),
                replace(kept, agent="other"),
                replace(kept, created_at=at(99)),
            ):
                with pytest.raises(InvalidValueError):
                    await store.update_run(changed)

            assert await _stored(store) == kept
            # And the two it may change go through.
            await store.update_run(replace(kept, state=RunState.WAITING, started_at=at(2)))
            found = await _stored(store)
            assert (found.state, found.started_at) == (RunState.WAITING, at(2))

    @asyncio_test
    async def test_a_run_that_is_not_there_cannot_be_changed(self) -> None:
        async with self.opened() as store:
            with pytest.raises(RunNotFoundError):
                await store.update_run(run())

    @asyncio_test
    async def test_a_run_ends_with_the_event_that_says_so(self) -> None:
        async with self.opened() as store:
            await _begun(store)
            over_with = transition(await _stored(store), RunState.CANCELLED, now=at(4))
            event = over(RUN, FIRST_POSITION + 1, RunState.CANCELLED)

            await store.end_run(over_with, event, run_event_to_data(event))

            found = await store.run_by_id(RUN)
            assert found is not None and found.state is RunState.CANCELLED
            assert await store.active_run_of(CONVERSATION) is None
            assert [document["seq"] for document in await store.events_of(RUN)] == [
                FIRST_POSITION,
                FIRST_POSITION + 1,
            ]

    @asyncio_test
    async def test_a_run_that_finished_keeps_the_state_the_engine_left(self) -> None:
        """The memory of a conversation is what the engine handed back with the
        run's ending: stored with it, in one write, handed back as it was given
        and never read (``docs/specs/conversations.md``, "The model's memory")."""
        async with self.opened() as store:
            await _begun(store)
            over_with = transition(await _stored(store), RunState.FINISHED, now=at(4))
            event = over(RUN, FIRST_POSITION + 1, RunState.FINISHED)
            given = bytearray(b'{"messages": [1, 2]}')

            await store.end_run(
                over_with, event, run_event_to_data(event), engine_state=bytes(given)
            )
            given[0:1] = b"["

            assert await store.engine_state(RUN) == b'{"messages": [1, 2]}'

    @asyncio_test
    async def test_a_run_ended_without_a_state_and_one_still_going_have_none(self) -> None:
        async with self.opened() as store:
            await _begun(store)
            assert await store.engine_state(RUN) is None
            await _ended(store, RunState.CANCELLED)

            assert await store.engine_state(RUN) is None
            assert await store.engine_state(uuid.uuid4()) is None

    @asyncio_test
    async def test_a_state_is_bytes_and_nothing_is_written_for_anything_else(self) -> None:
        async with self.opened() as store:
            await _begun(store)
            over_with = transition(await _stored(store), RunState.FINISHED, now=at(4))
            event = over(RUN, FIRST_POSITION + 1, RunState.FINISHED)

            with pytest.raises(InvalidValueError, match="bytes"):
                await store.end_run(
                    over_with,
                    event,
                    run_event_to_data(event),
                    engine_state="{}",  # type: ignore[arg-type]
                )

            assert (await _stored(store)).state is RunState.RUNNING
            assert await store.last_position(RUN) == FIRST_POSITION

    @asyncio_test
    async def test_a_run_that_has_ended_is_never_written_again(self) -> None:
        # What stops a process coming back from a timeout and re-opening a run
        # its author cancelled a moment ago.
        async with self.opened() as store:
            await _begun(store)
            going = await _stored(store)
            await _ended(store, RunState.CANCELLED)

            with pytest.raises(IllegalTransitionError):
                await store.update_run(going)
            again = over(RUN, FIRST_POSITION + 2, RunState.FINISHED)
            with pytest.raises(IllegalTransitionError):
                await store.end_run(
                    transition(going, RunState.FINISHED, now=at(9)),
                    again,
                    run_event_to_data(again),
                )

            found = await store.run_by_id(RUN)
            assert found is not None and found.state is RunState.CANCELLED
            assert await store.last_position(RUN) == FIRST_POSITION + 1

    @asyncio_test
    async def test_a_run_that_has_not_ended_does_not_end_through_end_run(self) -> None:
        async with self.opened() as store:
            await _begun(store)
            event = over(RUN, FIRST_POSITION + 1, RunState.FINISHED)

            with pytest.raises(InvalidValueError):
                await store.end_run(await _stored(store), event, run_event_to_data(event))

            assert await store.last_position(RUN) == FIRST_POSITION

    @asyncio_test
    async def test_an_end_at_the_wrong_position_writes_neither_half(self) -> None:
        async with self.opened() as store:
            await _begun(store)
            event = over(RUN, FIRST_POSITION + 7, RunState.FINISHED)

            with pytest.raises(PositionTakenError):
                await store.end_run(
                    transition(await _stored(store), RunState.FINISHED, now=at(4)),
                    event,
                    run_event_to_data(event),
                )

            found = await store.run_by_id(RUN)
            assert found is not None and found.state is RunState.RUNNING
            assert await store.last_position(RUN) == FIRST_POSITION

    # Events.

    @asyncio_test
    async def test_an_event_document_is_kept_whole_and_handed_back_as_it_was(self) -> None:
        async with self.opened() as store:
            await _begun(store, event=False)
            event = started(RUN)
            document = run_event_to_data(event)

            await store.append_event(event, document)

            assert list(await store.events_of(RUN)) == [document]
            assert await store.last_position(RUN) == FIRST_POSITION

    @asyncio_test
    async def test_positions_follow_one_another_with_no_gaps(self) -> None:
        async with self.opened() as store:
            await _begun(store)

            for seq in (FIRST_POSITION, FIRST_POSITION + 2):
                with pytest.raises(PositionTakenError):
                    await _appended(store, delta(RUN, seq))

            await _appended(store, delta(RUN, FIRST_POSITION + 1))
            assert await store.last_position(RUN) == FIRST_POSITION + 1

    @asyncio_test
    async def test_events_come_back_after_the_position_a_watcher_last_saw(self) -> None:
        async with self.opened() as store:
            await _begun(store)
            for seq in range(FIRST_POSITION + 1, FIRST_POSITION + 4):
                await _appended(store, delta(RUN, seq, text=f"delta {seq}"))

            documents = await store.events_of(RUN, after=FIRST_POSITION + 1)

            assert [document["seq"] for document in documents] == [
                FIRST_POSITION + 2,
                FIRST_POSITION + 3,
            ]
            assert await store.events_of(RUN, after=99) == ()
            with pytest.raises(InvalidValueError):
                await store.events_of(RUN, after=-1)

    @asyncio_test
    async def test_events_can_be_read_up_to_a_position_as_well_as_past_one(self) -> None:
        """Both ends, because whoever replays the beginning of a stream wants one.

        A re-attached stream is built on the events **before** the position it
        carries on from (``application.Watch.before``), and reading the whole
        run to throw the end of it away is a read that grows with the answer.
        """
        async with self.opened() as store:
            await _begun(store)
            for seq in range(FIRST_POSITION + 1, FIRST_POSITION + 4):
                await _appended(store, delta(RUN, seq, text=f"delta {seq}"))

            upto = await store.events_of(RUN, upto=FIRST_POSITION + 1)
            between = await store.events_of(RUN, after=FIRST_POSITION, upto=FIRST_POSITION + 2)

            assert [document["seq"] for document in upto] == [
                FIRST_POSITION,
                FIRST_POSITION + 1,
            ]
            assert [document["seq"] for document in between] == [
                FIRST_POSITION + 1,
                FIRST_POSITION + 2,
            ]
            # Past the end is everything; below the beginning is nothing, and
            # neither is a refusal -- both are slices that ask for what they ask.
            assert await store.events_of(RUN, upto=99) == await store.events_of(RUN)
            assert await store.events_of(RUN, upto=0) == ()
            assert await store.events_of(RUN, after=3, upto=2) == ()
            with pytest.raises(InvalidValueError):
                await store.events_of(RUN, upto=-1)

    @asyncio_test
    async def test_an_event_of_a_run_that_is_not_there_is_refused(self) -> None:
        async with self.opened() as store:
            with pytest.raises(RunNotFoundError):
                await _appended(store, started(OTHER_RUN))

    @asyncio_test
    async def test_one_runs_events_are_not_anothers(self) -> None:
        async with self.opened() as store:
            await _begun(store)
            await _begun(store, run_id=OTHER_RUN, conversation_id=OTHER_CONVERSATION)

            documents = await store.events_of(RUN)

            assert [document["run_id"] for document in documents] == [str(RUN)]

    @asyncio_test
    async def test_what_a_caller_holds_is_never_what_the_store_holds_of_an_event(self) -> None:
        async with self.opened() as store:
            await _begun(store, event=False)
            event = started(RUN)
            document = run_event_to_data(event)
            await store.append_event(event, document)

            document["event"]["conversation_id"] = "mutated afterwards"
            (read,) = await store.events_of(RUN)
            read["event"]["conversation_id"] = "mutated on the way out"

            (again,) = await store.events_of(RUN)
            assert again["event"]["conversation_id"] == str(CONVERSATION)

    # Completing a message.

    @asyncio_test
    async def test_a_message_and_its_announcement_are_stored_together(self) -> None:
        async with self.opened() as store:
            asked = await _begun(store)
            replied = answer(asked, seconds=3)
            event = completed(RUN, FIRST_POSITION + 1, replied)

            await store.complete_message(
                replied,
                message_to_data(replied),
                event,
                run_event_to_data(event),
                now=at(3),
            )

            assert len(await store.messages_of(CONVERSATION)) == 2
            assert await store.last_position(RUN) == FIRST_POSITION + 1
            found = await store.conversation_by_id(CONVERSATION)
            assert found is not None
            assert found.updated_at == at(3)

    @asyncio_test
    async def test_a_tool_message_completes_as_an_answer_does_and_reads_back(self) -> None:
        """The one tool message of a batch is stored with the event that says so,
        under the answer that made the calls, and carries no provenance: it is
        the platform's own (``docs/specs/runs.md``, "Tools")."""
        async with self.opened() as store:
            asked = await _begun(store)
            calling = answer(
                asked,
                seconds=3,
                parts=(TextPart("Let me look."), ToolCallPart("toolu_01", "github__search", {})),
            )
            await store.complete_message(
                calling,
                message_to_data(calling),
                completed(RUN, FIRST_POSITION + 1, calling),
                run_event_to_data(completed(RUN, FIRST_POSITION + 1, calling)),
                now=at(3),
            )
            results = Message(
                id=uuid.uuid4(),
                conversation_id=CONVERSATION,
                parent_id=calling.id,
                role=Role.TOOL,
                parts=(ToolResultPart("toolu_01", "found 3"),),
                created_at=at(4),
            )
            event = completed(RUN, FIRST_POSITION + 2, results)

            await store.complete_message(
                results, message_to_data(results), event, run_event_to_data(event), now=at(4)
            )

            documents = await store.messages_of(CONVERSATION)
            assert [message_from_data(document) for document in documents][-2:] == [
                calling,
                results,
            ]
            assert await store.last_position(RUN) == FIRST_POSITION + 2

    @asyncio_test
    async def test_a_question_is_never_completed_by_a_run(self) -> None:
        async with self.opened() as store:
            asked = await _begun(store)
            again = question("and?", parent=asked, seconds=3)
            event = completed(RUN, FIRST_POSITION + 1, again)

            with pytest.raises(InvalidValueError, match="question"):
                await store.complete_message(
                    again, message_to_data(again), event, run_event_to_data(event), now=at(3)
                )

    @asyncio_test
    async def test_a_completion_at_the_wrong_position_stores_neither_half(self) -> None:
        async with self.opened() as store:
            asked = await _begun(store)
            replied = answer(asked, seconds=3)
            event = completed(RUN, FIRST_POSITION + 5, replied)

            with pytest.raises(PositionTakenError):
                await store.complete_message(
                    replied, message_to_data(replied), event, run_event_to_data(event), now=at(3)
                )

            assert len(await store.messages_of(CONVERSATION)) == 1
            assert await store.last_position(RUN) == FIRST_POSITION

    @asyncio_test
    async def test_a_completion_of_a_message_that_cannot_be_stored_announces_nothing(self) -> None:
        async with self.opened() as store:
            await _begun(store)
            stray = answer(uuid.uuid4(), seconds=3)
            event = completed(RUN, FIRST_POSITION + 1, stray)

            with pytest.raises(MessageNotFoundError):
                await store.complete_message(
                    stray, message_to_data(stray), event, run_event_to_data(event), now=at(3)
                )

            assert len(await store.messages_of(CONVERSATION)) == 1
            assert await store.last_position(RUN) == FIRST_POSITION

    @asyncio_test
    async def test_an_ended_run_takes_no_more_events_and_completes_nothing(self) -> None:
        # A writer that read the last position, was cancelled, and wrote
        # afterwards would put an event past the RunEnded that is there.
        async with self.opened() as store:
            asked = await _begun(store)
            await _ended(store, RunState.CANCELLED)
            replied = answer(asked, seconds=3)
            event = completed(RUN, FIRST_POSITION + 2, replied)

            with pytest.raises(IllegalTransitionError):
                await _appended(store, delta(RUN, FIRST_POSITION + 2))
            with pytest.raises(IllegalTransitionError):
                await store.complete_message(
                    replied, message_to_data(replied), event, run_event_to_data(event), now=at(3)
                )

            assert await store.last_position(RUN) == FIRST_POSITION + 1
            assert len(await store.messages_of(CONVERSATION)) == 1
            _readable(await store.events_of(RUN), follows=asked.id)

    @asyncio_test
    async def test_a_run_ends_with_an_event_that_says_what_the_record_says(self) -> None:
        async with self.opened() as store:
            await _begun(store)
            kept = await _stored(store)
            over_with = transition(kept, RunState.CANCELLED, now=at(4))

            wrong_kind = RunEvent(
                run_id=RUN,
                seq=FIRST_POSITION + 1,
                event=MessageCompleted(run_id=RUN, message=answer(uuid.uuid4(), seconds=3)),
            )
            with pytest.raises(InvalidValueError):
                await store.end_run(over_with, wrong_kind, run_event_to_data(wrong_kind))
            wrong_state = over(RUN, FIRST_POSITION + 1, RunState.FINISHED)
            with pytest.raises(InvalidValueError):
                await store.end_run(over_with, wrong_state, run_event_to_data(wrong_state))

            found = await _stored(store)
            assert found.state is RunState.RUNNING
            assert await store.last_position(RUN) == FIRST_POSITION

    @asyncio_test
    async def test_a_completed_message_and_its_announcement_must_agree(self) -> None:
        async with self.opened() as store:
            asked = await _begun(store)
            elsewhere = await _begun(
                store, run_id=OTHER_RUN, conversation_id=OTHER_CONVERSATION, event=False
            )
            replied = answer(asked, seconds=3)
            position = FIRST_POSITION + 1

            # A run of one conversation completing a message into another.
            stray = answer(elsewhere, seconds=3, conversation_id=OTHER_CONVERSATION)
            crossed = completed(RUN, position, stray)
            with pytest.raises(InvalidValueError):
                await store.complete_message(
                    stray, message_to_data(stray), crossed, run_event_to_data(crossed), now=at(3)
                )

            # An announcement carrying another message.
            other = completed(RUN, position, answer(asked, "another", seconds=3))
            with pytest.raises(InvalidValueError):
                await store.complete_message(
                    replied, message_to_data(replied), other, run_event_to_data(other), now=at(3)
                )

            # An event that is not a completion at all.
            wrong_kind = delta(RUN, position)
            with pytest.raises(InvalidValueError):
                await store.complete_message(
                    replied,
                    message_to_data(replied),
                    wrong_kind,
                    run_event_to_data(wrong_kind),
                    now=at(3),
                )

            # A question, or an answer another run produced.
            asked_again = question("not an answer", parent=asked, seconds=3)
            as_question = completed(RUN, position, asked_again)
            with pytest.raises(InvalidValueError):
                await store.complete_message(
                    asked_again,
                    message_to_data(asked_again),
                    as_question,
                    run_event_to_data(as_question),
                    now=at(3),
                )
            theirs = answer(asked, seconds=3, provenance=provenance(run_id=OTHER_RUN))
            mine = completed(RUN, position, theirs)
            with pytest.raises(InvalidValueError):
                await store.complete_message(
                    theirs, message_to_data(theirs), mine, run_event_to_data(mine), now=at(3)
                )

            assert len(await store.messages_of(CONVERSATION)) == 1
            assert await store.last_position(RUN) == FIRST_POSITION

    @asyncio_test
    async def test_a_snapshot_carries_the_run_in_flight_and_its_events(self) -> None:
        async with self.opened() as store:
            asked = await _begun(store)
            replied = answer(asked, seconds=3)
            await _completed(store, replied, FIRST_POSITION + 1)

            snapshot = await store.conversation_snapshot(CONVERSATION)

            assert snapshot.active_run is not None and snapshot.active_run.id == RUN
            assert [document["id"] for document in snapshot.messages] == [
                str(asked.id),
                str(replied.id),
            ]
            assert [document["seq"] for document in snapshot.events] == [
                FIRST_POSITION,
                FIRST_POSITION + 1,
                FIRST_POSITION + 2,
            ]
            _readable(snapshot.events, follows=asked.id)

    @asyncio_test
    async def test_a_snapshot_of_a_conversation_whose_run_has_ended_carries_no_run(self) -> None:
        async with self.opened() as store:
            await _begun(store)
            await _ended(store, RunState.FINISHED)

            snapshot = await store.conversation_snapshot(CONVERSATION)

            assert (snapshot.active_run, snapshot.events) == (None, ())
            assert snapshot.conversation is not None

    # Deleting.

    @asyncio_test
    async def test_deleting_takes_the_runs_and_their_events_with_it(self) -> None:
        async with self.opened() as store:
            await _begun(store)
            await _ended(store, RunState.FINISHED)
            await _begun(store, run_id=OTHER_RUN, conversation_id=OTHER_CONVERSATION)

            assert await store.delete_conversation(CONVERSATION, now=at(9))

            assert await store.run_by_id(RUN) is None
            assert await store.events_of(RUN) == ()
            held = await self.dump(store)
            assert str(RUN) not in held
            # And the conversation beside it keeps its run and its events.
            assert str(OTHER_RUN) in held

    @asyncio_test
    async def test_a_conversation_that_is_answering_is_not_deleted(self) -> None:
        async with self.opened() as store:
            await _begun(store)

            with pytest.raises(RunAlreadyActiveError):
                await store.delete_conversation(CONVERSATION, now=at(9))

            assert await store.conversation_by_id(CONVERSATION) is not None
            assert await store.run_by_id(RUN) is not None
            assert await store.last_position(RUN) == FIRST_POSITION

    # Two things at once.

    @asyncio_test
    async def test_one_of_several_runs_started_at_once_is_the_one_that_runs(self) -> None:
        async with self.opened() as store:
            await store.add_conversation(conversation())
            asked = question(seconds=0)
            await store.append_message(asked, message_to_data(asked), now=at(1))
            starting = [run(id=uuid.uuid4(), message_id=asked.id) for _ in range(5)]

            outcomes = await asyncio.gather(
                *(
                    store.start_run(conversation=None, message=None, run=kept, now=at(2))
                    for kept in starting
                ),
                return_exceptions=True,
            )

            assert sum(1 for outcome in outcomes if outcome is None) == 1
            assert all(
                isinstance(outcome, RunAlreadyActiveError)
                for outcome in outcomes
                if outcome is not None
            )
            going = await store.active_run_of(CONVERSATION)
            assert going is not None
            stored = [kept for kept in starting if await store.run_by_id(kept.id) is not None]
            assert [kept.id for kept in stored] == [going.id]

    @asyncio_test
    async def test_one_of_several_events_offered_a_position_at_once_is_stored(self) -> None:
        async with self.opened() as store:
            await _begun(store, event=False)

            outcomes = await asyncio.gather(
                *(
                    store.append_event(
                        delta(RUN, FIRST_POSITION, text=f"delta {n}"),
                        run_event_to_data(delta(RUN, FIRST_POSITION, text=f"delta {n}")),
                    )
                    for n in range(5)
                ),
                return_exceptions=True,
            )

            assert sum(1 for outcome in outcomes if outcome is None) == 1
            assert all(
                isinstance(outcome, PositionTakenError)
                for outcome in outcomes
                if outcome is not None
            )
            assert len(await store.events_of(RUN)) == 1
            assert await store.last_position(RUN) == FIRST_POSITION

    @asyncio_test
    async def test_a_delete_and_a_run_beginning_at_once_leave_no_orphan(self) -> None:
        # Either the conversation went and the run was refused because it is
        # not there, or the run began and the delete was refused because the
        # conversation is answering. Never both, and never a run whose
        # conversation is gone.
        async with self.opened() as store:
            await store.add_conversation(conversation())
            asked = question(seconds=0)
            await store.append_message(asked, message_to_data(asked), now=at(1))

            deleted, started_it = await asyncio.gather(
                store.delete_conversation(CONVERSATION, now=at(9)),
                store.start_run(
                    conversation=None, message=None, run=run(message_id=asked.id), now=at(2)
                ),
                return_exceptions=True,
            )

            gone = await store.conversation_by_id(CONVERSATION) is None
            kept_run = await store.run_by_id(RUN)
            if gone:
                assert deleted is True
                # The losing `start_run` breaks more than one rule at once --
                # the conversation is not there, and neither is the message it
                # answers -- and which of them a store meets first is
                # deliberately unspecified.
                assert isinstance(started_it, NotFoundError)
                assert kept_run is None
            else:
                assert started_it is None
                assert isinstance(deleted, RunAlreadyActiveError)
                assert kept_run is not None

    @asyncio_test
    async def test_two_completions_offered_a_position_at_once_store_one_whole_answer(
        self,
    ) -> None:
        # Both halves of the loser must be gone: a message stored without its
        # event is a message no watcher is told about.
        async with self.opened() as store:
            asked = await _begun(store)
            first = answer(asked, "one answer", seconds=3)
            second = answer(asked, "another answer", seconds=3)
            events = [completed(RUN, FIRST_POSITION + 1, kept) for kept in (first, second)]

            outcomes = await asyncio.gather(
                *(
                    store.complete_message(
                        kept, message_to_data(kept), event, run_event_to_data(event), now=at(3)
                    )
                    for kept, event in zip((first, second), events, strict=True)
                ),
                return_exceptions=True,
            )

            assert sum(1 for outcome in outcomes if outcome is None) == 1
            assert len(await store.messages_of(CONVERSATION)) == 2
            assert await store.last_position(RUN) == FIRST_POSITION + 1
            held = await self.dump(store)
            lost = second if outcomes[0] is None else first
            assert str(lost.id) not in held

    @asyncio_test
    async def test_a_completion_and_an_end_at_once_leave_a_readable_stream(self) -> None:
        # Whichever wins, what is in the events table must still be a stream
        # `core.check_event_order` can read back.
        async with self.opened() as store:
            asked = await _begun(store)
            replied = answer(asked, seconds=3)
            await _appended(store, announced(RUN, FIRST_POSITION + 1, replied))
            done = completed(RUN, FIRST_POSITION + 2, replied)
            stopped = over(RUN, FIRST_POSITION + 2, RunState.CANCELLED)
            ending = transition(await _stored(store), RunState.CANCELLED, now=at(4))

            outcomes = await asyncio.gather(
                store.complete_message(
                    replied, message_to_data(replied), done, run_event_to_data(done), now=at(3)
                ),
                store.end_run(ending, stopped, run_event_to_data(stopped)),
                return_exceptions=True,
            )

            assert sum(1 for outcome in outcomes if outcome is None) == 1
            assert await store.last_position(RUN) == FIRST_POSITION + 2
            _readable(await store.events_of(RUN), follows=asked.id)
            kept = await _stored(store)
            # The message is stored if and only if its announcement is.
            completed_it = outcomes[0] is None
            assert (len(await store.messages_of(CONVERSATION)) == 2) is completed_it
            assert (kept.state is RunState.CANCELLED) is not completed_it

    @asyncio_test
    async def test_a_snapshot_taken_while_a_message_completes_holds_both_or_neither(
        self,
    ) -> None:
        # The compound READ, held to what the compound writes are: a message
        # is in the snapshot's messages if and only if its completion is in
        # its events. A store that reads the two separately shows a tree
        # without the answer and a replay that begins after it -- which is
        # the gap somebody opening the conversation sees.
        for turns, snapshot_first in _INTERLEAVINGS:
            async with self.opened() as store:
                asked = await _begun(store)
                replied = answer(asked, seconds=3)
                await _appended(store, announced(RUN, FIRST_POSITION + 1, replied))
                event = completed(RUN, FIRST_POSITION + 2, replied)

                snapshot = await _raced(
                    store.complete_message(
                        replied,
                        message_to_data(replied),
                        event,
                        run_event_to_data(event),
                        now=at(3),
                    ),
                    store.conversation_snapshot(CONVERSATION),
                    turns=turns,
                    snapshot_first=snapshot_first,
                )

                _whole(snapshot, replied, where=(turns, snapshot_first))

    @asyncio_test
    async def test_a_snapshot_taken_while_a_run_ends_shows_it_going_or_not_at_all(self) -> None:
        # An ended run is not in a snapshot at all, so a consistent one is
        # either the run with events that do not yet end it, or no run and no
        # events. A run whose events already say it is over is the tear.
        for turns, snapshot_first in _INTERLEAVINGS:
            async with self.opened() as store:
                await _begun(store)
                stopped = over(RUN, FIRST_POSITION + 1, RunState.CANCELLED)
                ending = transition(await _stored(store), RunState.CANCELLED, now=at(4))

                snapshot = await _raced(
                    store.end_run(ending, stopped, run_event_to_data(stopped)),
                    store.conversation_snapshot(CONVERSATION),
                    turns=turns,
                    snapshot_first=snapshot_first,
                )

                _settled(snapshot, where=(turns, snapshot_first))

    @asyncio_test
    async def test_a_rename_and_a_completion_at_once_keep_both(self) -> None:
        # `complete_message` dates the conversation as `append_message` does,
        # so it meets a rename on the same row in the same way.
        async with self.opened() as store:
            asked = await _begun(store)
            replied = answer(asked, seconds=3)
            await _appended(store, announced(RUN, FIRST_POSITION + 1, replied))
            event = completed(RUN, FIRST_POSITION + 2, replied)

            await asyncio.gather(
                store.rename_conversation(CONVERSATION, "Renamed", now=at(5)),
                store.complete_message(
                    replied, message_to_data(replied), event, run_event_to_data(event), now=at(6)
                ),
            )

            found = await store.conversation_by_id(CONVERSATION)
            assert found is not None
            assert found.title == "Renamed"

    @asyncio_test
    async def test_two_conversations_may_start_a_run_at_once(self) -> None:
        async with self.opened() as store:
            here = question(seconds=0)
            there = question(conversation_id=OTHER_CONVERSATION, seconds=0)

            await asyncio.gather(
                store.start_run(
                    conversation=conversation(),
                    message=(here, message_to_data(here)),
                    run=run(message_id=here.id),
                    now=at(1),
                ),
                store.start_run(
                    conversation=conversation(id=OTHER_CONVERSATION),
                    message=(there, message_to_data(there)),
                    run=run(id=OTHER_RUN, conversation_id=OTHER_CONVERSATION, message_id=there.id),
                    now=at(1),
                ),
            )

            assert await store.active_run_of(CONVERSATION) is not None
            assert await store.active_run_of(OTHER_CONVERSATION) is not None


async def _begun(
    store: ConversationStore,
    *,
    run_id: uuid.UUID = RUN,
    conversation_id: uuid.UUID = CONVERSATION,
    created_at: object = None,
    event: bool = True,
) -> Message:
    """A conversation with a question and a run answering it; its question.

    With the run's first event appended unless a test wants to append that
    one itself.
    """
    asked = question(conversation_id=conversation_id, seconds=0)
    changes: dict[str, object] = {
        "id": run_id,
        "conversation_id": conversation_id,
        "message_id": asked.id,
    }
    if created_at is not None:
        changes["created_at"] = created_at
    await store.start_run(
        conversation=conversation(id=conversation_id),
        message=(asked, message_to_data(asked)),
        run=run(**changes),
        now=at(1),
    )
    if event:
        await _appended(store, started(run_id, conversation_id=conversation_id))
    return asked


async def _stored(store: ConversationStore, run_id: uuid.UUID = RUN) -> Run:
    """The run as the store has it: what a change to it must be built from."""
    found = await store.run_by_id(run_id)
    assert found is not None
    return found


async def _ended(store: ConversationStore, state: RunState, *, run_id: uuid.UUID = RUN) -> None:
    """End that run with the announcement that goes with it."""
    position = await store.last_position(run_id) + 1
    event = over(run_id, position, state)
    kept = await _stored(store, run_id)
    await store.end_run(transition(kept, state, now=at(8)), event, run_event_to_data(event))


async def _appended(store: ConversationStore, event: RunEvent) -> None:
    """Append an event as the application does: the document, and the record."""
    await store.append_event(event, run_event_to_data(event))


async def _completed(
    store: ConversationStore,
    message: Message,
    seq: int,
    *,
    run_id: uuid.UUID = RUN,
) -> None:
    """Announce a message and complete it, as a turn does: two events, one message."""
    await _appended(store, announced(run_id, seq, message))
    event = completed(run_id, seq + 1, message)
    await store.complete_message(
        message, message_to_data(message), event, run_event_to_data(event), now=message.created_at
    )


_INTERLEAVINGS = [(turns, first) for turns in range(4) for first in (True, False)]
"""Every way of sliding a read across a write, on a loop that is one thread.

The event loop runs what is ready in the order it was scheduled, so which of
a writer's statements a reader falls between is decided by how many turns it
waited first and by which of the two was scheduled first. Walking all of them
is what makes a race test say something rather than happen to pass.

**Exhaustive against a cooperative store, probabilistic against a database.**
Over the in-memory store the schedule is the whole of what can happen, so a
store that tears is caught every time. Over PostgreSQL these are eight
attempts at a race whose timing belongs to the server: a correct store passes
them always, and a torn one may pass them by luck, so a green database run is
weaker evidence than a green run against the fake. The fake is where the
device has teeth; the database run is where the SQL is shown to mean it.
"""


async def _raced[T](
    writing: Awaitable[None], reading: Awaitable[T], *, turns: int, snapshot_first: bool
) -> T:
    """Run a read against a write at one interleaving, and give back what it read."""

    async def delayed() -> T:
        for _ in range(turns):
            await asyncio.sleep(0)
        return await reading

    if snapshot_first:
        read, _ = await asyncio.gather(delayed(), writing)
    else:
        _, read = await asyncio.gather(writing, delayed())
    return read


def _whole(snapshot: Snapshot, message: Message, *, where: object = None) -> None:
    """The message is in the snapshot if and only if its completion is."""
    stored = any(document["id"] == str(message.id) for document in snapshot.messages)
    told = any(
        isinstance(event.event, MessageCompleted) and event.event.message.id == message.id
        for event in (run_event_from_stored(document) for document in snapshot.events)
    )
    assert stored is told, (
        f"a torn snapshot at {where}: the message is"
        f" {'in' if stored else 'not in'} it and its completion is"
        f" {'in' if told else 'not in'} it"
    )


def _settled(snapshot: Snapshot, *, where: object = None) -> None:
    """A snapshot never shows a run whose events already say it is over."""
    events = [run_event_from_stored(document) for document in snapshot.events]
    if snapshot.active_run is None:
        assert snapshot.events == (), f"a torn snapshot at {where}: no run, and events with it"
    else:
        assert not any(
            isinstance(event.event, RunEnded) for event in events
        ), f"a torn snapshot at {where}: the run is going and its events end it"


def _readable(
    documents: tuple[Document, ...],
    *,
    follows: uuid.UUID,
    run_id: uuid.UUID = RUN,
    conversation_id: uuid.UUID = CONVERSATION,
) -> None:
    """The stored stream, read back and held to ``core.check_event_order``.

    A contract suite may import ``core`` -- it is a test, not a store. This is
    the check the store's own rules exist to make possible: whatever refusal
    or race happened, what is in the events table is a stream that can be read
    back.
    """
    events = [run_event_from_stored(document) for document in documents]
    check_event_order(
        events,
        run_id=run_id,
        conversation_id=conversation_id,
        follows=follows,
        ended=bool(events) and isinstance(events[-1].event, RunEnded),
    )
