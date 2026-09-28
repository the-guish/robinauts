# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Executing a turn: what is published, what is stored, and how it ends.

``application.Turns.execute`` over the fakes (``tests/turns.py``), driven by an
engine whose every step the test decides -- so nothing here sleeps, and every
assertion is about a turn that is exactly where the test wanted it.

**Every scenario ends by reading the stored stream back.** ``readable`` is
``core.check_event_order`` over the documents as the store holds them, and it
is asked in every one of them -- finished, failed, cancelled, timed out, swept,
and interrupted in the middle by somebody else -- because a run whose events
cannot be read back is a run no watcher can follow, however right the record
looks.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Sequence
from typing import Any

import pytest

from aio import asyncio_test
from conversations import AGENT, MODEL, OTHER_MODEL, agent_definition, at, offered
from fakes import CountingIdSource, FakeClock, Gate, MemoryConversationStore, Raise, calls, says
from robinauts.adapters import AsyncioRunExecutor, MemoryRunSignals
from robinauts.application import NO_TOOLS_YET, Conversations, Turns, Watch
from robinauts.application.turns import _Pump
from robinauts.core import (
    check_event_order,
    run_event_to_data,
    transition,
)
from robinauts.domain import (
    ACTIVE_RUN_STATES,
    ENDED_RUN_STATES,
    FIRST_POSITION,
    AgentDefinition,
    AnswerCompleted,
    AnswerReasoningDelta,
    AnswerStarted,
    AnswerTextDelta,
    IllegalTransitionError,
    Message,
    MessageCompleted,
    MessageStarted,
    PositionTakenError,
    ReasoningDelta,
    ReasoningPart,
    Role,
    Run,
    RunAlreadyActiveError,
    RunEnded,
    RunEvent,
    RunNotFoundError,
    RunState,
    TextDelta,
    TextPart,
    ToolCallPart,
    ToolDefinition,
    text_parts,
)
from robinauts.ports import MAX_SWEPT, Agent
from turns import (
    AUTHOR,
    NOW,
    SOMEBODY_ELSE,
    Wiring,
    begun,
    kinds,
    readable,
    settled,
    stored_events,
    stored_messages,
    submitted,
    wired,
    written,
)

ANSWER = "Someone who plays fair."

BOUND = 5.0
"""How long a test waits for something that must happen at once.

Not a measurement: the difference between "the run was ended although the
engine was still letting go" and "it waited for the engine", where the engine
in these tests never finishes.
"""


# --- a turn that runs to its end ---------------------------------------------


@asyncio_test
async def test_a_streamed_answer_is_announced_streamed_stored_and_announced_again() -> None:
    wiring = wired(*says(ANSWER, pieces=3))
    run = await begun(wiring)

    await wiring.turns.execute(run)

    events = await stored_events(wiring.store, run.id)
    readable(events, run)
    assert kinds(events) == [
        "RunStarted",
        "MessageStarted",
        "TextDelta",
        "TextDelta",
        "TextDelta",
        "MessageCompleted",
        "RunEnded",
    ]
    stored = await stored_messages(wiring.store, run.conversation_id)
    answered = stored[-1]
    # What was published, joined, is what was stored: character for character.
    published = "".join(event.event.text for event in events if isinstance(event.event, TextDelta))
    assert published == answered.text == ANSWER
    assert answered.parent_id == run.message_id
    assert answered.role is Role.ASSISTANT
    assert answered.provenance == run.provenance
    ended = await wiring.store.run_by_id(run.id)
    assert ended.state is RunState.FINISHED
    assert ended.error is None
    assert ended.started_at is not None and ended.finished_at is not None


@asyncio_test
async def test_an_answer_that_was_never_streamed_is_stored_all_the_same() -> None:
    wiring = wired(*says(ANSWER, streamed=False))
    run = await begun(wiring)

    await wiring.turns.execute(run)

    events = await stored_events(wiring.store, run.id)
    readable(events, run)
    assert kinds(events) == ["RunStarted", "MessageStarted", "MessageCompleted", "RunEnded"]
    assert (await stored_messages(wiring.store, run.conversation_id))[-1].text == ANSWER


@asyncio_test
async def test_two_answers_in_one_turn_hang_one_under_the_other() -> None:
    wiring = wired(*says("First."), *says("Second."))
    run = await begun(wiring)

    await wiring.turns.execute(run)

    events = await stored_events(wiring.store, run.id)
    readable(events, run)
    question, first, second = await stored_messages(wiring.store, run.conversation_id)
    assert (first.text, second.text) == ("First.", "Second.")
    # A turn is a chain: the first answer under the question, the next under
    # the answer before it.
    assert first.parent_id == question.id
    assert second.parent_id == first.id
    assert (await wiring.store.run_by_id(run.id)).state is RunState.FINISHED


@asyncio_test
async def test_reasoning_is_kept_in_the_runs_events_and_in_no_message() -> None:
    """Where reasoning lives, exactly (``docs/specs/conversations.md``).

    It is published as it arrives **and it is in the run's events**, like
    every other delta, because a watcher re-attaching in the middle of an
    answer has to be able to rebuild what it is watching. What it is not in is
    the **conversation**: no message holds any of it, so it is never sent back
    to a model and is in no export, and the run's events are removed once
    nobody can re-attach to them any more.
    """
    wiring = wired(
        AnswerStarted(),
        AnswerReasoningDelta(text="Thinking about it."),
        AnswerTextDelta(text=ANSWER),
        AnswerCompleted(parts=(ReasoningPart("Thinking about it."), TextPart(ANSWER))),
    )
    run = await begun(wiring)

    await wiring.turns.execute(run)

    events = await stored_events(wiring.store, run.id)
    readable(events, run)
    assert [event.event.text for event in events if isinstance(event.event, ReasoningDelta)] == [
        "Thinking about it."
    ]
    # In the stored event document, which is what a re-attaching watcher is
    # replayed, word for word.
    assert "Thinking about it." in repr(await wiring.store.events_of(run.id))
    # And in no message of the conversation, which is the record that lasts.
    stored = await stored_messages(wiring.store, run.conversation_id)
    assert stored[-1].parts == (TextPart(ANSWER),)
    assert not [
        part for message in stored for part in message.parts if isinstance(part, ReasoningPart)
    ]


@asyncio_test
async def test_an_answer_that_was_only_thinking_is_stored_as_an_empty_message() -> None:
    wiring = wired(
        AnswerStarted(),
        AnswerReasoningDelta(text="Thought better of it."),
        AnswerCompleted(parts=(ReasoningPart("Thought better of it."),)),
    )
    run = await begun(wiring)

    await wiring.turns.execute(run)

    readable(await stored_events(wiring.store, run.id), run)
    # A message always has content, and a turn the agent answered nothing to
    # is something a conversation should record rather than leave out.
    assert (await stored_messages(wiring.store, run.conversation_id))[-1].parts == (TextPart(""),)
    assert (await wiring.store.run_by_id(run.id)).state is RunState.FINISHED


@asyncio_test
async def test_a_character_that_arrives_in_two_halves_is_published_whole() -> None:
    wiring = wired(
        AnswerStarted(),
        AnswerTextDelta(text="a\ud83d"),
        AnswerTextDelta(text="\ude00b"),
        AnswerCompleted(parts=(TextPart("a\U0001f600b"),)),
    )
    run = await begun(wiring)

    await wiring.turns.execute(run)

    events = await stored_events(wiring.store, run.id)
    readable(events, run)
    published = [event.event.text for event in events if isinstance(event.event, TextDelta)]
    # The high half was held back until its other half arrived: nothing
    # published is half a character, and the pieces still add up.
    assert published == ["a", "\U0001f600b"]
    assert (await stored_messages(wiring.store, run.conversation_id))[-1].text == "a\U0001f600b"


@asyncio_test
async def test_an_answer_longer_than_one_part_is_stored_in_several(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("robinauts.domain.conversation.MAX_PART_CHARS", 4)
    wiring = wired(*says("abcdefghij"))
    run = await begun(wiring)

    await wiring.turns.execute(run)

    readable(await stored_events(wiring.store, run.id), run)
    answered = (await stored_messages(wiring.store, run.conversation_id))[-1]
    assert answered.parts == (TextPart("abcd"), TextPart("efgh"), TextPart("ij"))
    assert answered.text == "abcdefghij"


# --- a turn that ends badly ---------------------------------------------------


@asyncio_test
async def test_an_engine_that_raises_before_any_answer_fails_the_run() -> None:
    wiring = wired(Raise(RuntimeError("the provider said no")))
    run = await begun(wiring)

    await wiring.turns.execute(run)

    events = await stored_events(wiring.store, run.id)
    readable(events, run)
    assert kinds(events) == ["RunStarted", "RunEnded"]
    ended = await wiring.store.run_by_id(run.id)
    assert ended.state is RunState.FAILED
    # The type and what it said -- and nothing that looks like a traceback.
    assert ended.error == "RuntimeError: the provider said no"
    assert "\n" not in ended.error
    assert events[-1].event.error == ended.error
    assert len(await stored_messages(wiring.store, run.conversation_id)) == 1


@asyncio_test
async def test_an_engine_that_raises_in_the_middle_leaves_the_answer_uncompleted() -> None:
    wiring = wired(
        AnswerStarted(),
        AnswerTextDelta(text="As far as it g"),
        Raise(RuntimeError("cut off")),
    )
    run = await begun(wiring)

    await wiring.turns.execute(run)

    events = await stored_events(wiring.store, run.id)
    # A message announced and never completed is exactly what this looks
    # like, and the stream still reads back.
    readable(events, run)
    assert kinds(events) == ["RunStarted", "MessageStarted", "TextDelta", "RunEnded"]
    assert (await wiring.store.run_by_id(run.id)).state is RunState.FAILED
    assert len(await stored_messages(wiring.store, run.conversation_id)) == 1


@asyncio_test
async def test_an_engine_that_raises_after_an_answer_keeps_what_was_produced() -> None:
    wiring = wired(*says(ANSWER), Raise(RuntimeError("and then it fell over")))
    run = await begun(wiring)

    await wiring.turns.execute(run)

    events = await stored_events(wiring.store, run.id)
    readable(events, run)
    # What was complete is in the conversation; the run says it went wrong.
    assert [message.text for message in await stored_messages(wiring.store, run.conversation_id)][
        -1
    ] == ANSWER
    assert (await wiring.store.run_by_id(run.id)).state is RunState.FAILED


@asyncio_test
async def test_a_turn_that_produced_no_answer_is_a_failed_run() -> None:
    wiring = wired()
    run = await begun(wiring)

    await wiring.turns.execute(run)

    events = await stored_events(wiring.store, run.id)
    readable(events, run)
    ended = await wiring.store.run_by_id(run.id)
    assert ended.state is RunState.FAILED
    assert ended.error == "the agent produced no answer"


@asyncio_test
async def test_a_turn_that_stopped_in_the_middle_of_an_answer_is_a_failed_run() -> None:
    # The engine returned without completing what it announced and without
    # raising: not a finish, since a finished run leaves nothing half-written.
    wiring = wired(AnswerStarted(), AnswerTextDelta(text="half of it"))
    run = await begun(wiring)

    await wiring.turns.execute(run)

    events = await stored_events(wiring.store, run.id)
    readable(events, run)
    ended = await wiring.store.run_by_id(run.id)
    assert ended.state is RunState.FAILED
    assert ended.error == "the agent began an answer and never completed it"


@asyncio_test
async def test_an_answer_that_did_not_complete_with_what_it_streamed_fails_the_run() -> None:
    # The promise a watcher is given is that what arrived is what is stored;
    # an engine that breaks it is a failure, not a stream nobody can check.
    wiring = wired(
        AnswerStarted(),
        AnswerTextDelta(text="what you saw"),
        AnswerCompleted(parts=(TextPart("something else"),)),
    )
    run = await begun(wiring)

    await wiring.turns.execute(run)

    events = await stored_events(wiring.store, run.id)
    readable(events, run)
    assert (await wiring.store.run_by_id(run.id)).state is RunState.FAILED
    assert len(await stored_messages(wiring.store, run.conversation_id)) == 1


@asyncio_test
async def test_a_turn_that_takes_too_long_is_failed_and_says_so() -> None:
    held = Gate()
    wiring = wired(AnswerStarted(), AnswerTextDelta(text="Half of"), held, turn_seconds=0.25)
    run = await begun(wiring)

    turn = asyncio.create_task(wiring.turns.execute(run))
    # The engine is at the gate nobody opens and everything before it is
    # stored; what ends the turn is the timeout and not the test.
    await held.reached.wait()
    await written(wiring.store, run.id, 3)
    await turn

    events = await stored_events(wiring.store, run.id)
    readable(events, run)
    assert kinds(events) == ["RunStarted", "MessageStarted", "TextDelta", "RunEnded"]
    ended = await wiring.store.run_by_id(run.id)
    assert ended.state is RunState.FAILED
    assert ended.error == "the turn took longer than this deployment allows and was stopped"
    assert len(await stored_messages(wiring.store, run.conversation_id)) == 1
    assert wiring.agent.released == 1


# --- cancelling ---------------------------------------------------------------


@asyncio_test
async def test_cancelling_in_the_middle_of_an_answer_ends_the_run_and_keeps_nothing() -> None:
    held = Gate()
    wiring = wired(AnswerStarted(), AnswerTextDelta(text="Half of an"), held)
    run = await begun(wiring)
    # Through the executor, which is where the work of a run lives and what
    # `cancel` asks: a request begins a turn exactly this way.
    submitted(wiring, run)
    await held.reached.wait()
    await written(wiring.store, run.id, 3)

    asked = await wiring.turns.cancel(AUTHOR, run.id)

    # Asking is not the same as having happened: the work writes the end.
    assert asked.state is RunState.RUNNING
    await settled(wiring, run)
    events = await stored_events(wiring.store, run.id)
    readable(events, run)
    assert kinds(events) == ["RunStarted", "MessageStarted", "TextDelta", "RunEnded"]
    assert events[-1].event.state is RunState.CANCELLED
    ended = await wiring.store.run_by_id(run.id)
    assert ended.state is RunState.CANCELLED
    assert ended.error is None
    # The answer in flight was never completed, so it is not in the
    # conversation; the question is all there is.
    assert len(await stored_messages(wiring.store, run.conversation_id)) == 1
    # And the engine was let go of.
    assert wiring.agent.released == 1


@asyncio_test
async def test_cancelling_a_run_no_task_here_is_executing_ends_it_in_the_store() -> None:
    # What a restart looks like: the run is in the database and the process
    # that was executing it is gone.
    wiring = wired(*says(ANSWER))
    run = await begun(wiring)

    ended = await wiring.turns.cancel(AUTHOR, run.id)

    assert ended.state is RunState.CANCELLED
    events = await stored_events(wiring.store, run.id)
    # Its stream had nothing in it at all, and it still reads back: whoever
    # ends a run writes the beginning it never got.
    readable(events, run)
    assert kinds(events) == ["RunStarted", "RunEnded"]
    assert events[0].seq == FIRST_POSITION

    # And the task that was about to execute it finds it ended at its very
    # first write, and stops there without a word.
    await wiring.turns.execute(run)

    assert await stored_events(wiring.store, run.id) == events
    assert (await wiring.store.run_by_id(run.id)).state is RunState.CANCELLED


@asyncio_test
async def test_cancelling_a_run_that_has_already_ended_is_not_an_error() -> None:
    wiring = wired(*says(ANSWER))
    run = await begun(wiring)
    await wiring.turns.execute(run)

    ended = await wiring.turns.cancel(AUTHOR, run.id)

    assert ended.state is RunState.FINISHED
    assert kinds(await stored_events(wiring.store, run.id)).count("RunEnded") == 1


@asyncio_test
async def test_somebody_elses_run_is_answered_exactly_like_one_that_is_not_there() -> None:
    wiring = wired(*says(ANSWER))
    run = await begun(wiring)

    with pytest.raises(RunNotFoundError) as theirs:
        await wiring.turns.cancel(SOMEBODY_ELSE, run.id)
    with pytest.raises(RunNotFoundError) as missing:
        await wiring.turns.cancel(SOMEBODY_ELSE, uuid.uuid4())

    assert type(theirs.value) is type(missing.value)
    assert (await wiring.store.run_by_id(run.id)).state is RunState.RUNNING


@asyncio_test
async def test_a_run_ended_under_the_task_executing_it_stops_it_quietly() -> None:
    held = Gate()
    wiring = wired(AnswerStarted(), held, *says(ANSWER)[1:])
    run = await begun(wiring)
    turn = asyncio.create_task(wiring.turns.execute(run))
    await held.reached.wait()
    await written(wiring.store, run.id, 2)
    # Somebody else ends it: another process, or this one after a restart.
    position = await wiring.store.last_position(run.id) + 1
    ending = RunEvent(
        run_id=run.id, seq=position, event=RunEnded(run_id=run.id, state=RunState.CANCELLED)
    )
    await wiring.store.end_run(
        transition(run, RunState.CANCELLED, now=at(200)), ending, run_event_to_data(ending)
    )
    before = await stored_events(wiring.store, run.id)

    held.open()
    await turn

    # It stopped where it was. Nothing was written past the event that ended
    # the run, and nothing was raised at whoever scheduled it.
    after = await stored_events(wiring.store, run.id)
    assert after == before
    readable(after, run)
    assert (await wiring.store.run_by_id(run.id)).state is RunState.CANCELLED
    assert len(await stored_messages(wiring.store, run.conversation_id)) == 1


# --- the start-up sweep --------------------------------------------------------


@asyncio_test
async def test_the_sweep_interrupts_every_run_this_process_is_not_executing() -> None:
    wiring = wired(*says(ANSWER))
    left = await begun(wiring)
    elsewhere = (await wiring.turns.start(AUTHOR, agent_id=AGENT, text="Another chat.")).run

    swept = await wiring.turns.sweep_interrupted()

    assert {run.id for run in swept} == {left.id, elsewhere.id}
    for run in (left, elsewhere):
        ended = await wiring.store.run_by_id(run.id)
        assert ended.state is RunState.INTERRUPTED
        assert ended.finished_at is not None
        events = await stored_events(wiring.store, run.id)
        readable(events, run)
        # Whoever marks it appends the event that ends it, so a watcher of
        # one is told it is over rather than left waiting.
        assert events[-1].event.state is RunState.INTERRUPTED
    assert not await wiring.store.runs_in(ACTIVE_RUN_STATES)


@asyncio_test
async def test_the_sweep_leaves_the_runs_this_process_is_executing_alone() -> None:
    held = Gate()
    wiring = wired(AnswerStarted(), held, *says(ANSWER)[1:])
    run = await begun(wiring)
    turn = asyncio.create_task(wiring.turns.execute(run))
    await held.reached.wait()

    assert await wiring.turns.sweep_interrupted() == ()

    assert wiring.turns.executing == frozenset({run.id})
    held.open()
    await turn
    assert (await wiring.store.run_by_id(run.id)).state is RunState.FINISHED


class Raced(MemoryConversationStore):
    """A store where somebody else has always ended the run first."""

    async def end_run(self, run: Run, event: RunEvent, event_document: object) -> None:
        raise IllegalTransitionError(f"run {run.id} has ended and is not written again")


@asyncio_test
async def test_a_run_somebody_else_ended_first_is_not_a_failed_sweep() -> None:
    wiring = wired(*says(ANSWER), store=Raced())
    run = await begun(wiring)

    assert await wiring.turns.sweep_interrupted() == ()

    # It moved on to what it could not end, rather than raising at start-up.
    assert (await wiring.store.run_by_id(run.id)).state is RunState.RUNNING


@asyncio_test
async def test_the_sweep_does_not_interrupt_a_run_that_holds_no_process() -> None:
    # A `waiting` run is suspended on a tool call and nothing is executing it,
    # so there is nothing to have gone away. The state machine says so and the
    # sweep asks it rather than repeating it.
    wiring = wired(*says(ANSWER))
    run = await begun(wiring)
    await wiring.store.update_run(transition(run, RunState.WAITING, now=at(200)))

    assert await wiring.turns.sweep_interrupted() == ()

    assert (await wiring.store.run_by_id(run.id)).state is RunState.WAITING


# --- what a watcher sees --------------------------------------------------------


@asyncio_test
async def test_a_watcher_re_attaches_at_every_point_of_a_run() -> None:
    inside, between = Gate(), Gate()
    wiring = wired(
        AnswerStarted(),
        AnswerTextDelta(text="Someone who "),
        inside,
        AnswerTextDelta(text="plays fair."),
        AnswerCompleted(parts=text_parts(ANSWER)),
        between,
        *says("And that is all."),
    )
    run = await begun(wiring)
    turn = asyncio.create_task(wiring.turns.execute(run))

    # In the middle of the first answer: what is stored is the question, and
    # what is replayed is the message being produced, from its announcement.
    await inside.reached.wait()
    await written(wiring.store, run.id, 3)
    await _re_attaches(wiring, run, messages=1)
    inside.open()

    # After it completed: the answer is in the conversation now, and the
    # replay begins after the event that announced it.
    await between.reached.wait()
    await written(wiring.store, run.id, 5)
    await _re_attaches(wiring, run, messages=2)
    between.open()

    await turn
    readable(await stored_events(wiring.store, run.id), run)


async def _re_attaches(wiring: Wiring, run: Run, *, messages: int) -> None:
    """Open the conversation and check the slice a watcher would be sent.

    Exactly what the wire does (``docs/specs/runs.md``): load the messages
    that are complete, ask where to attach, and read the events after that
    point. What comes back must read as a slice in its own right -- numbered
    on from where it was asked for, hanging under what it was told -- and a
    run still going has not ended.
    """
    opened = await wiring.conversations.open(AUTHOR, run.conversation_id)

    assert opened.run_id == run.id
    assert len(opened.messages) == messages
    slice_of = await stored_events(wiring.store, run.id, after=opened.resume.after)
    check_event_order(
        slice_of,
        run_id=run.id,
        conversation_id=run.conversation_id,
        follows=opened.resume.follows,
        after=opened.resume.after,
        ended=False,
    )
    # Nothing already shown is replayed.
    replayed = {
        event.event.message.id for event in slice_of if isinstance(event.event, MessageCompleted)
    }
    assert not replayed & {message.id for message in opened.messages}


# --- what the engine is given ----------------------------------------------------


@asyncio_test
async def test_the_engine_is_given_the_agent_and_the_path_to_the_question() -> None:
    wiring = wired(*says(ANSWER))
    run = await begun(wiring)

    await wiring.turns.execute(run)

    asked = wiring.agent.asked[-1]
    assert asked.agent == wiring.definition
    assert [message.id for message in asked.history] == [run.message_id]


@asyncio_test
async def test_the_engine_is_given_the_model_the_conversation_was_moved_to() -> None:
    # Not the agent's default: the run's, which is the conversation's when the
    # run was begun -- and the answer says so.
    wiring = wired(*says(ANSWER))
    first = await begun(wiring)
    await wiring.turns.execute(first)
    await wiring.turns.set_model(AUTHOR, first.conversation_id, OTHER_MODEL)
    second = await wiring.turns.start(
        AUTHOR,
        conversation_id=first.conversation_id,
        text="And why?",
        parent_id=(await stored_messages(wiring.store, first.conversation_id))[-1].id,
    )

    await wiring.turns.execute(second.run)

    assert [asked.model for asked in wiring.agent.asked] == [MODEL, OTHER_MODEL]
    answers = [
        message.provenance.model
        for message in await stored_messages(wiring.store, first.conversation_id)
        if message.provenance is not None
    ]
    assert answers == [MODEL, OTHER_MODEL]


@asyncio_test
async def test_a_run_begun_before_the_model_was_changed_runs_on_the_one_it_began_with() -> None:
    # The change is for the next turn: the run in flight -- and one taken up
    # again later -- is handed the model it records, not the conversation's
    # as it now is.
    wiring = wired(*says(ANSWER))
    run = await begun(wiring)
    await wiring.turns.set_model(AUTHOR, run.conversation_id, OTHER_MODEL)

    await wiring.turns.execute(run)

    assert wiring.agent.asked[-1].model == MODEL
    answer = (await stored_messages(wiring.store, run.conversation_id))[-1]
    assert answer.provenance is not None and answer.provenance.model == MODEL


@asyncio_test
async def test_the_engine_is_handed_the_whole_path_and_no_tools_yet() -> None:
    """Nothing is trimmed above the port (ADR 0004): the second turn's engine
    sees the first question, its answer and the new question, in order. And
    the tools a run fetches from its agent's servers are the next step's."""
    wiring = wired(*says("A" * 20))
    first = await begun(wiring, "Q" * 20)
    await wiring.turns.execute(first)
    second = await wiring.turns.start(
        AUTHOR,
        conversation_id=first.conversation_id,
        text="R" * 20,
        parent_id=(await stored_messages(wiring.store, first.conversation_id))[-1].id,
    )

    await wiring.turns.execute(second.run)

    assert [message.text for message in wiring.agent.history] == ["Q" * 20, "A" * 20, "R" * 20]
    assert [message.role for message in wiring.agent.history] == [
        Role.USER,
        Role.ASSISTANT,
        Role.USER,
    ]
    assert wiring.agent.asked[-1].tools == ()


@asyncio_test
async def test_a_turn_that_asks_for_a_tool_fails_on_the_event_and_stores_no_half_answer() -> None:
    """The engines yield a call where they used to refuse one; until the loop
    runs it, the application fails the turn on the first word of it, so that
    nothing half-answered is stored (``docs/working-notes/mcp-plan.md``)."""
    wiring = wired(*calls(("toolu_01", "github__search", {"q": "x"}), text="Let me look."))
    run = await begun(wiring)

    await wiring.turns.execute(run)

    ended = await wiring.store.run_by_id(run.id)
    assert ended is not None and ended.state is RunState.FAILED
    assert ended.error is not None and NO_TOOLS_YET in ended.error
    events = await stored_events(wiring.store, run.id)
    readable(events, run)
    # The answer was announced and its text published, and never completed.
    assert kinds(events) == ["RunStarted", "MessageStarted", "TextDelta", "RunEnded"]
    assert [
        message.role for message in await stored_messages(wiring.store, run.conversation_id)
    ] == [Role.USER]


@asyncio_test
async def test_an_answer_completed_with_calls_but_never_announced_fails_the_turn_too() -> None:
    made = ToolCallPart("toolu_01", "github__search", {"q": "x"})
    wiring = wired(AnswerStarted(), AnswerCompleted(parts=(made,)))
    run = await begun(wiring)

    await wiring.turns.execute(run)

    ended = await wiring.store.run_by_id(run.id)
    assert ended is not None and ended.state is RunState.FAILED
    assert ended.error is not None and NO_TOOLS_YET in ended.error
    readable(await stored_events(wiring.store, run.id), run)
    assert [
        message.role for message in await stored_messages(wiring.store, run.conversation_id)
    ] == [Role.USER]


@asyncio_test
async def test_what_the_engine_hands_back_in_extras_is_stored_on_the_answer() -> None:
    """The vendor's signed blocks ride on the answer as data the platform
    never reads, so that the adapter that made them finds them in the history
    it is handed next turn (``docs/specs/conversations.md``, "extras")."""
    signed = {"anthropic": {"thinking": [{"type": "redacted_thinking", "data": "OPAQUE"}]}}
    wiring = wired(*says("Found it.", extras=signed))
    run = await begun(wiring)

    await wiring.turns.execute(run)

    stored = await stored_messages(wiring.store, run.conversation_id)
    assert [message.extras for message in stored] == [{}, signed]
    events = await stored_events(wiring.store, run.id)
    (completed,) = [event.event for event in events if isinstance(event.event, MessageCompleted)]
    assert completed.message.extras == signed


# --- ending a run, whatever is happening to the task ---------------------------


class SlowToAnswer(MemoryConversationStore):
    """A store that **commits** and then takes for ever to say that it did.

    What a connection dropping at the wrong moment looks like from up here:
    the write happened, the await never came back. A process that counted its
    own positions instead of reading them back is one behind from then on.
    """

    def __init__(self, *, after: int = 1) -> None:
        super().__init__()
        self.after = after
        self.hanging = asyncio.Event()
        self._writes = 0

    async def append_event(self, event: RunEvent, document: object) -> None:
        await super().append_event(event, document)  # type: ignore[arg-type]
        self._writes += 1
        if self._writes == self.after:
            self.hanging.set()
            await asyncio.Event().wait()


class HeldEnding(MemoryConversationStore):
    """A store whose ``end_run`` waits, so a test can cancel while it runs."""

    def __init__(self) -> None:
        super().__init__()
        self.ending = asyncio.Event()
        self.go = asyncio.Event()

    async def end_run(self, run: Run, event: RunEvent, event_document: object) -> None:
        self.ending.set()
        await self.go.wait()
        await super().end_run(run, event, event_document)  # type: ignore[arg-type]


class Unreachable(MemoryConversationStore):
    """A store that cannot be reached for the first ``failures`` endings."""

    def __init__(self, *, failures: int) -> None:
        super().__init__()
        self.failures = failures
        self.attempts = 0

    async def end_run(self, run: Run, event: RunEvent, event_document: object) -> None:
        self.attempts += 1
        if self.attempts <= self.failures:
            raise ConnectionError("the database is not answering")
        await super().end_run(run, event, event_document)  # type: ignore[arg-type]


@asyncio_test
async def test_a_write_that_committed_without_answering_does_not_lose_the_run() -> None:
    # The turn's timeout lands while the store is committing an event. The
    # position this process counted is one behind what is stored, and an
    # ending offered at it would be refused -- and read as "somebody ended it
    # first", leaving the run `running` for ever with its conversation
    # blocked behind it.
    wiring = wired(*says(ANSWER), store=SlowToAnswer(after=2), turn_seconds=0.25)
    run = await begun(wiring)

    turn = asyncio.create_task(wiring.turns.execute(run))
    await wiring.store.hanging.wait()  # type: ignore[attr-defined]
    await turn

    ended = await wiring.store.run_by_id(run.id)
    assert ended.state is RunState.FAILED
    assert ended.error == "the turn took longer than this deployment allows and was stopped"
    events = await stored_events(wiring.store, run.id)
    readable(events, run)
    assert kinds(events) == ["RunStarted", "MessageStarted", "RunEnded"]
    assert [event.seq for event in events] == [1, 2, 3]


@asyncio_test
async def test_a_cancel_landing_on_a_finished_ending_still_ends_the_run() -> None:
    # The answer is stored and the run is being finished when the author
    # cancels. Before, the cancellation propagated out of the write and the
    # run was never ended at all: the answer was in the conversation and the
    # conversation was blocked behind a run that said it was still going.
    wiring = wired(*says(ANSWER), store=HeldEnding())
    run = await begun(wiring)
    turn = asyncio.create_task(wiring.turns.execute(run))
    await wiring.store.ending.wait()  # type: ignore[attr-defined]

    turn.cancel()
    wiring.store.go.set()  # type: ignore[attr-defined]

    with pytest.raises(asyncio.CancelledError):
        await turn
    ended = await wiring.store.run_by_id(run.id)
    assert ended.state is RunState.FINISHED
    events = await stored_events(wiring.store, run.id)
    readable(events, run)
    assert events[-1].event.state is RunState.FINISHED
    assert len(await stored_messages(wiring.store, run.conversation_id)) == 2


@asyncio_test
async def test_a_cancel_landing_on_a_failed_ending_still_ends_the_run() -> None:
    wiring = wired(Raise(RuntimeError("the provider said no")), store=HeldEnding())
    run = await begun(wiring)
    turn = asyncio.create_task(wiring.turns.execute(run))
    await wiring.store.ending.wait()  # type: ignore[attr-defined]

    turn.cancel()
    wiring.store.go.set()  # type: ignore[attr-defined]

    with pytest.raises(asyncio.CancelledError):
        await turn
    ended = await wiring.store.run_by_id(run.id)
    assert ended.state is RunState.FAILED
    readable(await stored_events(wiring.store, run.id), run)


@asyncio_test
async def test_a_store_that_cannot_be_reached_is_tried_again() -> None:
    wiring = wired(*says(ANSWER), store=Unreachable(failures=2))
    run = await begun(wiring)

    await wiring.turns.execute(run)

    assert wiring.store.attempts == 3  # type: ignore[attr-defined]
    assert (await wiring.store.run_by_id(run.id)).state is RunState.FINISHED
    readable(await stored_events(wiring.store, run.id), run)


@asyncio_test
async def test_a_run_that_could_not_be_ended_says_so_in_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The known limit of this version: it stays `running` until the start-up
    # sweep of the next restart (``docs/specs/runs.md``).
    wiring = wired(*says(ANSWER), store=Unreachable(failures=99))
    run = await begun(wiring)

    with caplog.at_level(logging.ERROR):
        await wiring.turns.execute(run)

    assert (await wiring.store.run_by_id(run.id)).state is RunState.RUNNING
    assert "could not be ended" in caplog.text
    assert "start-up sweep" in caplog.text


# --- letting the engine go ------------------------------------------------------


class BreaksWhenLetGo(Agent):
    """An engine whose release fails -- while the turn is being cancelled."""

    def run_turn(
        self,
        agent: AgentDefinition,
        history: Sequence[Message],
        tools: Sequence[ToolDefinition],
        *,
        model: str,
    ) -> Any:
        return self._events()

    async def _events(self) -> Any:
        try:
            yield AnswerStarted()
            yield AnswerTextDelta(text="Half of an")
            await asyncio.Event().wait()
        finally:
            raise RuntimeError("the client would not close")


class NeverLetsGo(Agent):
    """An engine that takes a minute to let go, and will not be hurried.

    What a provider connection that is closing badly looks like: the
    ``finally`` runs, says so, and then waits -- through being cancelled, as
    often as it is cancelled -- until the test frees it.
    """

    def __init__(self) -> None:
        self.answering = asyncio.Event()
        self.closing = asyncio.Event()
        self.free = asyncio.Event()

    def run_turn(
        self,
        agent: AgentDefinition,
        history: Sequence[Message],
        tools: Sequence[ToolDefinition],
        *,
        model: str,
    ) -> Any:
        return self._events()

    async def _events(self) -> Any:
        try:
            yield AnswerStarted()
            yield AnswerTextDelta(text="Half of an")
            self.answering.set()
            await asyncio.Event().wait()
        finally:
            self.closing.set()
            while not self.free.is_set():
                with contextlib.suppress(asyncio.CancelledError):
                    await self.free.wait()


def over(engine: Agent) -> Wiring:
    """A wiring whose engine is that one."""
    definition = agent_definition()
    store = MemoryConversationStore()
    clock = FakeClock(now=NOW)
    executor = AsyncioRunExecutor()
    signals = MemoryRunSignals()
    return Wiring(
        turns=Turns(
            store=store,
            clock=clock,
            ids=CountingIdSource(),
            agents={definition.id: definition},
            models=offered(),
            engines={definition.engine: engine},
            executor=executor,
            signals=signals,
        ),
        conversations=Conversations(store=store, clock=clock),
        watch=Watch(store=store, signals=signals),
        executor=executor,
        signals=signals,
        store=store,
        clock=clock,
        ids=CountingIdSource(),
        agent=engine,  # type: ignore[arg-type]
        definition=definition,
    )


@asyncio_test
async def test_a_cancelled_turn_whose_engine_then_fails_is_still_cancelled() -> None:
    # The engine's `finally` raises as the cancellation unwinds it. Before,
    # that exception was read as an ordinary failure: the run ended `failed`
    # and `execute` returned normally although its task had been cancelled.
    wiring = over(BreaksWhenLetGo())
    run = await begun(wiring)
    turn = asyncio.create_task(wiring.turns.execute(run))
    await written(wiring.store, run.id, 3)

    turn.cancel()

    with pytest.raises(asyncio.CancelledError):
        await turn
    ended = await wiring.store.run_by_id(run.id)
    assert ended.state is RunState.CANCELLED
    assert ended.error is None
    readable(await stored_events(wiring.store, run.id), run)


@asyncio_test
async def test_a_cancelled_run_is_ended_although_its_engine_will_not_let_go(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The release is a minute long and ignores being cancelled. It happens in
    # the task that reads the engine, not in the one that is ending the run,
    # so the run is over long before the engine is -- and the engine is then
    # abandoned rather than waited for.
    monkeypatch.setattr("robinauts.application.turns.CLOSING_SECONDS", 0.05)
    engine = NeverLetsGo()
    wiring = over(engine)
    run = await begun(wiring)
    turn = asyncio.create_task(wiring.turns.execute(run))
    await engine.answering.wait()
    await written(wiring.store, run.id, 3)

    turn.cancel()

    try:
        # The bound is the whole point: with the engine read in this task,
        # this would wait on a release that never finishes.
        with caplog.at_level(logging.WARNING):
            async with asyncio.timeout(BOUND):
                with pytest.raises(asyncio.CancelledError):
                    await turn
        assert engine.closing.is_set()
        # Given its time and then let go of, rather than waited for.
        assert "abandoned" in caplog.text
        ended = await wiring.store.run_by_id(run.id)
        assert ended.state is RunState.CANCELLED
        events = await stored_events(wiring.store, run.id)
        readable(events, run)
        assert kinds(events) == ["RunStarted", "MessageStarted", "TextDelta", "RunEnded"]
    finally:
        await _freed(engine)


@asyncio_test
async def test_a_turn_that_timed_out_ends_although_its_engine_will_not_let_go(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("robinauts.application.turns.CLOSING_SECONDS", 0.05)
    engine = NeverLetsGo()
    wiring = over(engine)
    wiring.turns._turn_seconds = 0.1  # noqa: SLF001 - the limit under test
    run = await begun(wiring)

    try:
        async with asyncio.timeout(BOUND):
            await wiring.turns.execute(run)

        assert engine.closing.is_set()
        ended = await wiring.store.run_by_id(run.id)
        assert ended.state is RunState.FAILED
        assert ended.error == "the turn took longer than this deployment allows and was stopped"
        readable(await stored_events(wiring.store, run.id), run)
    finally:
        await _freed(engine)


async def _freed(engine: NeverLetsGo) -> None:
    """Let the abandoned engine finish, so the loop closes on nothing."""
    engine.free.set()
    for _ in range(10):
        await asyncio.sleep(0)


# --- one execution per run, and claiming one before its task exists ---------------


@asyncio_test
async def test_a_run_is_executed_once_by_this_process() -> None:
    held = Gate()
    wiring = wired(AnswerStarted(), held, *says(ANSWER)[1:])
    run = await begun(wiring)
    turn = asyncio.create_task(wiring.turns.execute(run))
    await held.reached.wait()

    with pytest.raises(RunAlreadyActiveError):
        await wiring.turns.execute(run)

    held.open()
    await turn
    readable(await stored_events(wiring.store, run.id), run)


@asyncio_test
async def test_a_claimed_run_is_not_swept_before_its_task_has_begun() -> None:
    # The window the sweep must not fall into: the executor has created the
    # task and the event loop has not stepped it yet.
    wiring = wired(*says(ANSWER))
    run = await begun(wiring)

    wiring.turns.claim(run.id)

    assert wiring.turns.executing == frozenset({run.id})
    assert await wiring.turns.sweep_interrupted() == ()
    with pytest.raises(RunAlreadyActiveError):
        wiring.turns.claim(run.id)
    # And a claim whose task was never created is given up.
    wiring.turns.let_go(run.id)
    assert wiring.turns.executing == frozenset()
    assert {ended.id for ended in await wiring.turns.sweep_interrupted()} == {run.id}


@asyncio_test
async def test_a_claim_is_taken_up_by_the_execution_it_was_made_for() -> None:
    wiring = wired(*says(ANSWER))
    run = await begun(wiring)
    wiring.turns.claim(run.id)

    await wiring.turns.execute(run)

    assert (await wiring.store.run_by_id(run.id)).state is RunState.FINISHED
    assert wiring.turns.executing == frozenset()


# --- what a run records about itself ----------------------------------------------


@asyncio_test
async def test_a_run_records_when_it_began_before_it_records_anything_else() -> None:
    held = Gate()
    wiring = wired(AnswerStarted(), held, *says(ANSWER)[1:])
    run = await begun(wiring)

    turn = asyncio.create_task(wiring.turns.execute(run))
    await held.reached.wait()

    # While it is still going: an operator can see how long it has been
    # running, rather than being told when it ends.
    going = await wiring.store.run_by_id(run.id)
    assert going.started_at == NOW
    assert going.state is RunState.RUNNING
    wiring.clock.advance(30)
    held.open()
    await turn

    ended = await wiring.store.run_by_id(run.id)
    assert ended.started_at == NOW
    assert ended.finished_at == at(130)
    assert ended.started_at < ended.finished_at


@asyncio_test
async def test_what_an_engine_raised_reaches_the_log_escaped_and_bounded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A provider's exception carries whatever the provider sent: newlines,
    # control characters, megabytes. Written out raw it would be a record of
    # what happened whose shape somebody else chose.
    said = "line one\nline two\r\n" + "x" * 5_000
    wiring = wired(Raise(RuntimeError(said)))
    run = await begun(wiring)

    with caplog.at_level(logging.WARNING):
        await wiring.turns.execute(run)

    lines = [record.getMessage() for record in caplog.records]
    assert len(lines) == 1
    assert "\n" not in lines[0] and "\r" not in lines[0]
    assert len(lines[0]) < 2_000
    assert "RuntimeError" in lines[0]
    assert str(run.id) in lines[0]
    # And the run itself keeps a sentence, not a traceback.
    ended = await wiring.store.run_by_id(run.id)
    assert ended.error.startswith("RuntimeError: line one")
    assert len(ended.error) <= 2_000


@asyncio_test
async def test_a_turn_executed_where_there_is_no_task_is_still_ended_and_forgotten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # There is always a task in practice; this is about the bookkeeping not
    # falling over when there is not one to register, and leaving nothing
    # behind for the sweep to trip on.
    wiring = wired(*says(ANSWER))
    run = await begun(wiring)
    monkeypatch.setattr(asyncio, "current_task", lambda: None)

    await wiring.turns.execute(run)

    assert (await wiring.store.run_by_id(run.id)).state is RunState.FINISHED
    assert wiring.turns.executing == frozenset()
    readable(await stored_events(wiring.store, run.id), run)


# --- two writers ------------------------------------------------------------------


class Sluggish(MemoryConversationStore):
    """A store where every call takes several turns of the loop.

    Enough for two callers to be inside it at once at every step, which is
    what a real one does all the time and what an in-memory one never does.
    """

    @staticmethod
    async def _slowly() -> None:
        for _ in range(3):
            await asyncio.sleep(0)

    async def append_event(self, event: RunEvent, document: object) -> None:
        await self._slowly()
        await super().append_event(event, document)  # type: ignore[arg-type]

    async def end_run(self, run: Run, event: RunEvent, event_document: object) -> None:
        await self._slowly()
        await super().end_run(run, event, event_document)  # type: ignore[arg-type]

    async def last_position(self, run_id: uuid.UUID) -> int:
        await self._slowly()
        return await super().last_position(run_id)

    async def events_of(self, run_id: uuid.UUID, *, after: int = 0) -> tuple[Any, ...]:
        await self._slowly()
        return await super().events_of(run_id, after=after)


@asyncio_test
async def test_a_cancel_and_the_task_it_raced_start_the_run_once() -> None:
    # The two writers there can be: a cancel that found no task to cancel and
    # so is ending the run itself, and the task that was about to execute it.
    # Both may find an empty stream and both may decide it needs the event
    # that begins a run -- and a run started twice is a stream nothing can
    # read back.
    wiring = wired(*says(ANSWER), store=Sluggish())
    run = await begun(wiring)
    # Submitted and not yet begun, which is the window: the executor has the
    # work, nothing has run it, and `cancel` therefore ends the run in the
    # store while the work is about to start writing into it.
    submitted(wiring, run)

    asked = await wiring.turns.cancel(AUTHOR, run.id)
    await settled(wiring, run)

    events = await stored_events(wiring.store, run.id)
    assert kinds(events).count("RunStarted") == 1
    readable(events, run)
    ended = await wiring.store.run_by_id(run.id)
    assert ended.state in ENDED_RUN_STATES
    # Whichever of the two got there, what the cancel answered is either the
    # state the run ended in or that it was still going when it looked. It
    # never invents one.
    assert asked.state is ended.state or asked.is_active


@asyncio_test
async def test_the_task_and_a_sweep_that_raced_it_start_the_run_once() -> None:
    # The mirror: the start-up sweep is ending a run it thinks nobody has,
    # while the task that has it begins.
    wiring = wired(*says(ANSWER), store=Sluggish())
    run = await begun(wiring)

    sweeping = asyncio.create_task(wiring.turns.sweep_interrupted())
    executing = asyncio.create_task(wiring.turns.execute(run))
    await asyncio.gather(sweeping, executing)

    events = await stored_events(wiring.store, run.id)
    assert kinds(events).count("RunStarted") == 1
    assert kinds(events).count("RunEnded") == 1
    readable(events, run)
    assert (await wiring.store.run_by_id(run.id)).state in ENDED_RUN_STATES


class Contrary(MemoryConversationStore):
    """A store that refuses a position although the run has not moved on.

    Nobody ended the run and nothing else is in its stream: the position is
    simply refused. A writer that read that as "somebody ended it first"
    would walk away from a run that is still `running`.
    """

    def __init__(self, *, refusals: int) -> None:
        super().__init__()
        self.left = refusals

    async def append_event(self, event: RunEvent, document: object) -> None:
        if self.left and isinstance(event.event, MessageStarted):
            self.left -= 1
            raise PositionTakenError(f"the next event is not {event.seq}")
        await super().append_event(event, document)  # type: ignore[arg-type]


@asyncio_test
async def test_a_stream_that_cannot_be_written_fails_the_run_rather_than_leaving_it() -> None:
    wiring = wired(*says(ANSWER), store=Contrary(refusals=3))
    run = await begun(wiring)

    await wiring.turns.execute(run)

    ended = await wiring.store.run_by_id(run.id)
    assert ended.state is RunState.FAILED
    assert ended.error == "the run's stream could not be written"
    events = await stored_events(wiring.store, run.id)
    readable(events, run)
    assert kinds(events) == ["RunStarted", "RunEnded"]


class NeverAnswers(MemoryConversationStore):
    """A store that takes the ending and does not come back.

    Freed at the end of the test, so that a regression is a failed assertion
    rather than a suite that hangs on its way out.
    """

    def __init__(self) -> None:
        super().__init__()
        self.free = asyncio.Event()

    async def end_run(self, run: Run, event: RunEvent, event_document: object) -> None:
        await self.free.wait()
        await super().end_run(run, event, event_document)  # type: ignore[arg-type]


@asyncio_test
async def test_an_ending_that_is_never_answered_is_given_up_on(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A store that refuses is one thing; one that never answers would hold the
    # task for ever, where nothing could even cancel it -- the executor could
    # never reap it and shutdown would never finish.
    monkeypatch.setattr("robinauts.application.turns.ENDING_SECONDS", 0.05)
    wiring = wired(*says(ANSWER), store=NeverAnswers())
    run = await begun(wiring)

    try:
        with caplog.at_level(logging.ERROR):
            turn = asyncio.create_task(wiring.turns.execute(run))
            # Not `asyncio.timeout`: the ending is shielded, so a test that
            # cancelled itself here would wait on it for ever. The question
            # is whether the task can be reaped at all.
            done, _ = await asyncio.wait({turn}, timeout=BOUND)

        assert done, "the ending was never given up on and the task could not be reaped"
        await turn
        assert "could not be ended" in caplog.text
        assert (await wiring.store.run_by_id(run.id)).state is RunState.RUNNING
    finally:
        wiring.store.free.set()  # type: ignore[attr-defined]


class Unhelpful(MemoryConversationStore):
    """A store that will not end one particular run, or talk about it."""

    def __init__(self) -> None:
        super().__init__()
        self.bad: uuid.UUID | None = None

    async def end_run(self, run: Run, event: RunEvent, event_document: object) -> None:
        if run.id == self.bad:
            raise ConnectionError("not answering about that one")
        await super().end_run(run, event, event_document)  # type: ignore[arg-type]

    async def run_by_id(self, run_id: uuid.UUID) -> Run | None:
        if run_id == self.bad:
            raise ConnectionError("not answering about that one")
        return await super().run_by_id(run_id)


@asyncio_test
async def test_one_run_that_cannot_be_swept_does_not_stop_the_sweep(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Start-up, over everything a process that went away left behind: a store
    # that will not answer about the first of them must not leave the rest
    # going.
    wiring = wired(*says(ANSWER), store=Unhelpful())
    left = await begun(wiring)
    elsewhere = (await wiring.turns.start(AUTHOR, agent_id=AGENT, text="Another chat.")).run
    wiring.store.bad = left.id  # type: ignore[attr-defined]

    with caplog.at_level(logging.ERROR):
        swept = await wiring.turns.sweep_interrupted()

    assert [run.id for run in swept] == [elsewhere.id]
    assert "could not be swept" in caplog.text
    readable(await stored_events(wiring.store, elsewhere.id), elsewhere)


# --- being cancelled twice, and the bookkeeping that must survive it -------------


@asyncio_test
async def test_a_second_cancellation_during_the_release_leaves_nothing_behind(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The window is real: the run has been ended and this task is waiting for
    # an engine that will not let go, which is exactly when a shutdown -- or a
    # second cancel that read the run a moment before it ended -- sends
    # another cancellation. It must not walk out of `execute` with the run
    # still registered as executing here: this process would then refuse to
    # execute it ever again and hide it from its own start-up sweep.
    monkeypatch.setattr("robinauts.application.turns.CLOSING_SECONDS", BOUND)
    unretrieved: list[object] = []
    asyncio.get_running_loop().set_exception_handler(
        lambda loop, context: unretrieved.append(context)
    )
    engine = NeverLetsGo()
    wiring = over(engine)
    run = await begun(wiring)
    submitted(wiring, run)
    await engine.answering.wait()
    await written(wiring.store, run.id, 3)

    try:
        asked = await wiring.turns.cancel(AUTHOR, run.id)
        # The engine's release has begun, so the work is inside it.
        with caplog.at_level(logging.WARNING):
            await engine.closing.wait()
            assert wiring.executor.cancel(run.id)

            await settled(wiring, run)

        assert asked.state is RunState.RUNNING
        assert (await wiring.store.run_by_id(run.id)).state is RunState.CANCELLED
        # Nothing held, and the engine given up on out loud.
        assert wiring.turns.executing == frozenset()
        assert "abandoned" in caplog.text
        readable(await stored_events(wiring.store, run.id), run)
    finally:
        await _freed(engine)
    assert unretrieved == []


class SlowToClose(Agent):
    """An engine whose ``finally`` waits, and lets a cancellation through."""

    def __init__(self) -> None:
        self.closing = asyncio.Event()
        self.free = asyncio.Event()

    def run_turn(
        self,
        agent: AgentDefinition,
        history: Sequence[Message],
        tools: Sequence[ToolDefinition],
        *,
        model: str,
    ) -> Any:
        return self._events()

    async def _events(self) -> Any:
        try:
            yield AnswerStarted()
            yield AnswerTextDelta(text="Half of an")
        finally:
            self.closing.set()
            await self.free.wait()


@asyncio_test
async def test_a_cancellation_while_a_stream_is_closing_is_not_swallowed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The other half of the same rule, on the path where the engine is closed
    # rather than cancelled: a task that returned normally from here although
    # it had been cancelled would tell whoever cancelled it that it had
    # finished of its own accord. Closing a stream nothing is iterating is
    # reached only from inside the release, so the release is what this
    # exercises.
    engine = SlowToClose()
    wiring = over(engine)
    events = engine.run_turn(wiring.definition, (), (), model=MODEL)
    assert await anext(events) == AnswerStarted()
    pump = _Pump()
    pump.events = events

    with caplog.at_level(logging.WARNING):
        releasing = asyncio.create_task(wiring.turns._released(pump))  # noqa: SLF001
        await engine.closing.wait()
        releasing.cancel()

        with pytest.raises(asyncio.CancelledError):
            await releasing

    assert "left closing" in caplog.text


# --- what a run that could not be ended is told ----------------------------------


class Obstinate(MemoryConversationStore):
    """A store that refuses every event's position, for ever, run still active."""

    async def append_event(self, event: RunEvent, document: object) -> None:
        raise PositionTakenError(f"the next event is not {event.seq}")


@asyncio_test
async def test_a_run_left_running_by_a_stream_it_cannot_write_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The stream cannot be written at all, so the run cannot even be ended:
    # this is the known limit, and it must read as the known limit rather
    # than as "somebody else is looking after it".
    wiring = wired(*says(ANSWER), store=Obstinate())
    run = await begun(wiring)

    with caplog.at_level(logging.WARNING):
        await wiring.turns.execute(run)

    assert (await wiring.store.run_by_id(run.id)).state is RunState.RUNNING
    assert "could not be ended" in caplog.text
    assert "start-up sweep" in caplog.text
    assert "the run's stream could not be written" in caplog.text


# --- the sweep reads what is ours after the listing -------------------------------


class SlowToList(MemoryConversationStore):
    """A store that takes its time over the listing the sweep begins with."""

    def __init__(self) -> None:
        super().__init__()
        self.listing = asyncio.Event()
        self.go = asyncio.Event()

    async def runs_in(self, states: Any, *, limit: int = MAX_SWEPT) -> tuple[Run, ...]:
        self.listing.set()
        await self.go.wait()
        return await super().runs_in(states, limit=limit)


@asyncio_test
async def test_a_run_claimed_while_the_sweep_was_listing_is_not_swept() -> None:
    # Start-up: the sweep asks the store what is going, and while it waits the
    # executor takes one of them up. The listing is older than the claim.
    wiring = wired(*says(ANSWER), store=SlowToList())
    run = await begun(wiring)
    sweeping = asyncio.create_task(wiring.turns.sweep_interrupted())
    await wiring.store.listing.wait()  # type: ignore[attr-defined]

    wiring.turns.claim(run.id)
    wiring.store.go.set()  # type: ignore[attr-defined]

    assert await sweeping == ()
    assert (await wiring.store.run_by_id(run.id)).state is RunState.RUNNING
    assert await wiring.store.events_of(run.id) == ()
