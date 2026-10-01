# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A turn in the background, and what the people watching it receive.

The three pieces of this step together, over the fakes (``tests/turns.py``):
``Turns.begin``, which writes the beginning of a turn and hands its work to
the ``RunExecutor``; the executor, which carries it while the request that
asked is long gone; and ``application.Watch``, which is what a watcher of that
run is sent.

**Every slice is checked as a slice.** ``core.check_event_order`` is asked of
what each watcher received, with the position it attached at and the message
the next announcement hangs under -- so "attach here and you have the rest of
it, once, in order" is asserted and not assumed. Where a watcher attached in
the middle of an answer it says which message that is, which is the one thing
a slice cannot work out for itself.

Nothing sleeps: the engine waits at gates the test opens, and the waiting is
for events the run itself sets.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

import pytest

from aio import asyncio_test
from conversations import AGENT
from fakes import Gate, MemoryConversationStore, calls, results, says
from robinauts.legacy.adapters import MemoryRunSignals
from robinauts.legacy.application import Watch
from robinauts.legacy.core import check_event_order
from robinauts.legacy.domain import (
    ACTIVE_RUN_STATES,
    Done,
    InvalidValueError,
    MessageStarted,
    Run,
    RunEnded,
    RunEvent,
    RunNotFoundError,
    RunQuietError,
    RunStarted,
    RunState,
    TextDelta,
    User,
)
from robinauts.legacy.ports import Document
from turns import (
    AUTHOR,
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
AGAIN = "And that is all."

LOOKED = ("toolu_01", "search", {"q": "robinauts"})
"""A call the first answer makes, as ``calls`` wants it.

What puts two answers in one turn now that the framework runs the loop: the
second follows the tool message that answers the first.
"""

FOUND = ("toolu_01", "search", "found 3")
"""What the tool came back with, as ``results`` wants it."""

SLOW = 0.5
"""How long one write of the slow store below takes."""

SHUTDOWN = 0.2
"""How long the shutdown below is given: a fraction of one of those writes."""

PATIENCE = 5.0
"""How long the test then waits for those writes to land. Never reached."""

MANY = 8
"""Runs caught in the instant before their first step, all at once."""

ASKED = 3
"""How often a watcher must have met failing signals for the test to read."""

BOUND = 2.0
"""How long a watcher that must finish by itself is given.

Not a measurement: every one of these ends because the run ended, the run
vanished or the silence ran out, and this is the difference between that and
a loop that never lets go.
"""


class Counted(MemoryConversationStore):
    """A store that counts the reads a watcher makes of a run's events.

    What tells "it finished" from "it finished by spinning": a watcher that
    reads without ever waiting takes the loop from the run it is watching, and
    the only visible difference is how often it asked.
    """

    def __init__(self) -> None:
        super().__init__()
        self.reads = 0

    async def events_of(
        self, run_id: uuid.UUID, *, after: int = 0, upto: int | None = None
    ) -> tuple[Document, ...]:
        self.reads += 1
        return await super().events_of(run_id, after=after, upto=upto)


class Deaf(MemoryRunSignals):
    """Signals that are never sent: every announcement is dropped.

    What a listener whose connection dropped looks like from above. A watcher
    over these is woken by nothing and must still receive the whole run, a
    bounded wait at a time -- which is the promise that keeps a lost signal
    from becoming a watcher that hangs.
    """

    async def announce(self, run_id: uuid.UUID, seq: int, *, ended: bool = False) -> None:
        return None


class Mute(MemoryRunSignals):
    """Signals that raise: a listener whose connection has dropped.

    The write side's announcements already survive one of these
    (``application.turns``); this is the read side, where a port that raised
    would otherwise end a stream the store could have gone on serving. It
    counts what it was asked, because how often a watcher tried is what says
    how often it said anything about it.
    """

    def __init__(self) -> None:
        super().__init__()
        self.asked = 0

    async def changed(self, run_id: uuid.UUID, after: int, *, timeout: float) -> bool:
        self.asked += 1
        raise OSError("the listener's connection has gone")


class Slow(MemoryConversationStore):
    """A store whose writes take their time: a database under strain.

    What makes a shutdown's own bound the thing that decides. The ending of a
    run is written under a shield -- no cancellation reaches it -- so a
    process that waited for this store would spend far more than the bound it
    was given, once for every run it was carrying.
    """

    async def last_position(self, run_id: uuid.UUID) -> int:
        await asyncio.sleep(SLOW)
        return await super().last_position(run_id)

    async def append_event(self, event: RunEvent, document: Document) -> None:
        await asyncio.sleep(SLOW)
        await super().append_event(event, document)

    async def end_run(
        self,
        run: Run,
        event: RunEvent,
        event_document: Document,
        *,
        engine_state: bytes | None = None,
    ) -> None:
        await asyncio.sleep(SLOW)
        await super().end_run(run, event, event_document, engine_state=engine_state)


def watching(
    wiring: Wiring, run: Run, *, after: int = 0, user: User = AUTHOR
) -> tuple[asyncio.Task[None], list[RunEvent]]:
    """Somebody watching that run from ``after``, and what they have received.

    A task, because a watcher runs beside the turn it is watching: a request
    holds one of these while the run writes into the store.
    """
    seen: list[RunEvent] = []
    events = wiring.watch.events(user, run.id, after=after)

    async def collect() -> None:
        async for event in events:
            seen.append(event)

    return asyncio.get_running_loop().create_task(collect()), seen


def is_the_end(seen: list[RunEvent], state: RunState) -> bool:
    """Whether the last thing that watcher was sent is the run ending so."""
    return isinstance(seen[-1].event, RunEnded) and seen[-1].event.state is state


# --- a turn that runs while nobody holds on to it ------------------------------


@asyncio_test
async def test_beginning_a_turn_answers_before_any_of_it_has_happened() -> None:
    wiring = wired(*says(ANSWER))

    started = await wiring.turns.begin(AUTHOR, agent_id=AGENT, text="What is a robinaut?")

    # The run exists and the work is scheduled; not a step of it has run.
    assert started.run.state is RunState.RUNNING
    assert await wiring.store.last_position(started.run.id) == 0
    assert started.run.id in wiring.turns.executing

    await settled(wiring, started.run)

    assert (await wiring.store.run_by_id(started.run.id)).state is RunState.FINISHED
    assert wiring.turns.executing == frozenset()
    readable(await stored_events(wiring.store, started.run.id), started.run)


@asyncio_test
async def test_beginning_again_answers_the_question_again_in_the_background() -> None:
    wiring = wired(*says(ANSWER))
    first = await wiring.turns.begin(AUTHOR, agent_id=AGENT, text="What is a robinaut?")
    await settled(wiring, first.run)
    answered = (await stored_messages(wiring.store, first.conversation.id))[-1]

    wiring.agent.steps = says(AGAIN)
    again = await wiring.turns.begin_again(
        AUTHOR, conversation_id=first.conversation.id, message_id=answered.id
    )
    await settled(wiring, again.run)

    assert (await wiring.store.run_by_id(again.run.id)).state is RunState.FINISHED
    readable(await stored_events(wiring.store, again.run.id), again.run)


@asyncio_test
async def test_a_turn_the_executor_will_not_take_is_ended_rather_than_left_going() -> None:
    # The one way a submission is refused: the process is stopping. The run
    # has been written by then, and a run nothing will ever execute would
    # block its conversation until the next start-up swept it.
    wiring = wired(*says(ANSWER))
    await wiring.executor.aclose(timeout=1.0)

    with pytest.raises(InvalidValueError):
        await wiring.turns.begin(AUTHOR, agent_id=AGENT, text="What is a robinaut?")

    assert await wiring.store.runs_in(ACTIVE_RUN_STATES) == ()
    page = await wiring.conversations.list_for(AUTHOR)
    opened = await wiring.conversations.open(AUTHOR, page.conversations[0].id)
    assert opened.ended_badly is not None
    assert opened.ended_badly.state is RunState.INTERRUPTED
    readable(await stored_events(wiring.store, opened.ended_badly.id), opened.ended_badly)


# --- watching from the beginning ----------------------------------------------


@asyncio_test
async def test_a_watcher_at_zero_receives_the_whole_run_and_then_ends() -> None:
    wiring = wired(*says(ANSWER, pieces=3))
    started = await wiring.turns.begin(AUTHOR, agent_id=AGENT, text="What is a robinaut?")
    watcher, seen = watching(wiring, started.run)

    await watcher

    readable(seen, started.run)
    assert kinds(seen) == [
        "RunStarted",
        "MessageStarted",
        "TextPiece",
        "TextPiece",
        "TextPiece",
        "MessageCompleted",
        "RunEnded",
    ]
    assert [event.seq for event in seen] == [1, 2, 3, 4, 5, 6, 7]
    # What was watched is what was stored, event for event.
    assert seen == await stored_events(wiring.store, started.run.id)


@asyncio_test
async def test_two_watchers_of_one_run_each_receive_all_of_it() -> None:
    wiring = wired(*says(ANSWER, pieces=2))
    started = await wiring.turns.begin(AUTHOR, agent_id=AGENT, text="What is a robinaut?")
    first, one = watching(wiring, started.run)
    second, two = watching(wiring, started.run)

    await asyncio.gather(first, second)

    readable(one, started.run)
    readable(two, started.run)
    assert one == two


# --- watching from where the conversation said --------------------------------


@asyncio_test
async def test_a_watcher_attaching_mid_run_receives_exactly_the_rest() -> None:
    between = Gate()
    wiring = wired(*calls(LOOKED, text=ANSWER), *results(FOUND), between, *says(AGAIN))
    run = await begun(wiring)
    submitted(wiring, run)
    # Stopped between the two answers: the first, with its call, and the tool
    # message that answers it are in the conversation, announced and
    # completed (positions 1 to 10).
    await between.reached.wait()
    await written(wiring.store, run.id, 10)

    opened = await wiring.conversations.open(AUTHOR, run.conversation_id)
    watcher, seen = watching(wiring, run, after=opened.resume.after)
    between.open()
    await watcher

    # The slice reads as a slice: numbered on from where it attached, hanging
    # under the message the conversation said the next one would follow.
    check_event_order(
        seen,
        run_id=run.id,
        conversation_id=run.conversation_id,
        follows=opened.resume.follows,
        after=opened.resume.after,
        ended=True,
    )
    assert kinds(seen) == ["MessageStarted", "TextPiece", "MessageCompleted", "RunEnded"]
    # And nothing the conversation already held was sent again.
    assert seen[0].seq == opened.resume.after + 1


@asyncio_test
async def test_a_watcher_attaching_inside_an_answer_is_given_the_rest_of_it() -> None:
    inside = Gate()
    wiring = wired(
        TextDelta(text="Someone who "),
        inside,
        TextDelta(text="plays fair."),
        Done(text=ANSWER),
    )
    run = await begun(wiring)
    submitted(wiring, run)
    await inside.reached.wait()
    await written(wiring.store, run.id, 3)
    stored = await stored_events(wiring.store, run.id)
    announced = next(
        event.event.message_id for event in stored if isinstance(event.event, MessageStarted)
    )

    # Attached in the middle of the answer, past the delta that was stored.
    watcher, seen = watching(wiring, run, after=3)
    inside.open()
    await watcher

    check_event_order(
        seen,
        run_id=run.id,
        conversation_id=run.conversation_id,
        follows=run.message_id,
        after=3,
        open_message=announced,
        ended=True,
    )
    assert kinds(seen) == ["TextPiece", "MessageCompleted", "RunEnded"]


# --- watching a run that is over ----------------------------------------------


@asyncio_test
async def test_a_watcher_of_a_run_that_has_ended_is_replayed_and_ends() -> None:
    wiring = wired(*says(ANSWER))
    started = await wiring.turns.begin(AUTHOR, agent_id=AGENT, text="What is a robinaut?")
    await settled(wiring, started.run)

    watcher, seen = watching(wiring, started.run)
    await watcher

    readable(seen, started.run)
    assert is_the_end(seen, RunState.FINISHED)


@asyncio_test
async def test_a_watcher_past_the_end_of_a_run_is_sent_nothing_and_ends() -> None:
    wiring = wired(*says(ANSWER))
    started = await wiring.turns.begin(AUTHOR, agent_id=AGENT, text="What is a robinaut?")
    await settled(wiring, started.run)
    last = await wiring.store.last_position(started.run.id)

    watcher, seen = watching(wiring, started.run, after=last)
    await watcher

    # Nothing to replay and nothing to wait for: a run that is over is over.
    assert seen == []
    # One position earlier is the end alone.
    ending, told = watching(wiring, started.run, after=last - 1)
    await ending
    assert is_the_end(told, RunState.FINISHED)


# --- a watcher that goes away --------------------------------------------------


@asyncio_test
async def test_a_watcher_that_goes_away_mid_run_leaves_nothing_behind() -> None:
    between = Gate()
    wiring = wired(*calls(LOOKED, text=ANSWER), *results(FOUND), between, *says(AGAIN))
    run = await begun(wiring)
    submitted(wiring, run)
    events = wiring.watch.events(AUTHOR, run.id)

    first = await anext(events)
    await events.aclose()

    assert isinstance(first.event, RunStarted)
    between.open()
    await settled(wiring, run)
    # The run neither noticed nor waited for it, and what is left of the
    # watcher that went is nothing: the run's own record is remembered for a
    # while, and nothing is waiting on it.
    assert (await wiring.store.run_by_id(run.id)).state is RunState.FINISHED
    readable(await stored_events(wiring.store, run.id), run)
    assert wiring.signals.tracked == frozenset({run.id})


@asyncio_test
async def test_watchers_of_a_run_that_is_over_all_finish_wherever_they_ask_from() -> None:
    # Every position, including the last and one past it, with two watchers on
    # each: a watcher of a run that has ended must **finish**, and it must not
    # get there by spinning -- a bound this short is not survivable by a loop
    # that reads without waiting.
    counted = Counted()
    wiring = wired(*says(ANSWER, pieces=2), store=counted)
    started = await wiring.turns.begin(AUTHOR, agent_id=AGENT, text="What is a robinaut?")
    await settled(wiring, started.run)
    last = await wiring.store.last_position(started.run.id)
    reads = counted.reads

    for after in range(0, last + 2):
        first, one = watching(wiring, started.run, after=after)
        second, two = watching(wiring, started.run, after=after)
        await asyncio.wait_for(asyncio.gather(first, second), timeout=BOUND)
        assert one == two
        assert [event.seq for event in one] == list(range(after + 1, last + 1))

    # And it took a handful of reads, not a storm of them: two watchers at
    # each of the positions, and at most a couple of reads each.
    assert counted.reads - reads <= 4 * (last + 2)


@asyncio_test
async def test_a_watcher_attached_past_everything_finishes_when_the_run_ends() -> None:
    # The shape that spins if the run is not looked at again after a wake-up:
    # a watcher parked **past** what this run will ever store, woken by the
    # end, reading nothing, and asking the signals again -- which answer at
    # once, for ever, because the run has ended. It must finish instead, and
    # in a handful of reads.
    held = Gate()
    counted = Counted()
    wiring = wired(TextDelta(text="Half of an"), held, store=counted)
    run = await begun(wiring)
    submitted(wiring, run)
    await held.reached.wait()
    await written(wiring.store, run.id, 3)
    watcher, seen = watching(wiring, run, after=99)
    await asyncio.sleep(0)
    reads = counted.reads

    await wiring.turns.cancel(AUTHOR, run.id)

    await asyncio.wait_for(watcher, timeout=BOUND)
    assert seen == []
    assert counted.reads - reads <= 4
    await settled(wiring, run)


@asyncio_test
async def test_a_watcher_of_a_run_that_is_no_longer_there_finishes() -> None:
    # The conversation was deleted while the watcher was parked. A run that is
    # gone is over: the stream ends, and there is nothing to report -- nothing
    # about the request was wrong.
    wiring = wired(*says(ANSWER), signals=Deaf(), wait_seconds=0.01)
    run = await begun(wiring)
    watcher, seen = watching(wiring, run)
    await asyncio.sleep(0)

    await wiring.turns.cancel(AUTHOR, run.id)
    await wiring.conversations.delete(AUTHOR, run.conversation_id)

    await asyncio.wait_for(watcher, timeout=BOUND)
    assert seen == []
    assert await wiring.store.run_by_id(run.id) is None


@asyncio_test
async def test_a_watcher_gives_up_on_a_run_that_stores_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A run whose end could not be written, or whose process was killed
    # elsewhere: nothing will ever be stored under it, and a watcher that
    # waited for ever would hold a request open for ever. Giving up is
    # **raised**: "we stopped watching" and "there is nothing more" are
    # different things to tell somebody, and telling them apart by what is
    # missing from a stream is how a page ends up waiting for an answer that
    # was never coming.
    wiring = wired(*says(ANSWER), wait_seconds=0.01, quiet_seconds=0.05)
    run = await begun(wiring)
    seen: list[RunEvent] = []

    with caplog.at_level(logging.WARNING), pytest.raises(RunQuietError):
        async with asyncio.timeout(BOUND):
            async for event in wiring.watch.events(AUTHOR, run.id):
                seen.append(event)

    assert seen == []
    assert "given up on" in caplog.text
    # The run is untouched: giving up on watching one is not ending it.
    assert (await wiring.store.run_by_id(run.id)).state is RunState.RUNNING


# --- signals that never arrive -------------------------------------------------


@asyncio_test
async def test_a_signal_that_is_lost_is_a_slower_stream_and_not_a_broken_one() -> None:
    # Every announcement dropped, and a bound short enough for the test to
    # wait out: the watcher reads anyway when it passes, and receives the run.
    wiring = wired(*says(ANSWER, pieces=2), signals=Deaf(), wait_seconds=0.01)
    started = await wiring.turns.begin(AUTHOR, agent_id=AGENT, text="What is a robinaut?")

    watcher, seen = watching(wiring, started.run)
    await watcher

    readable(seen, started.run)
    assert is_the_end(seen, RunState.FINISHED)


@asyncio_test
async def test_signals_that_raise_are_signals_that_are_not_there(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Waiting is how a watcher is prompt, not how it is right: what it sends
    # comes from the store. So a wait that raises is logged, waited out and
    # carried on from -- and the run arrives at the rate of the bound, which
    # is exactly what a lost announcement costs.
    wiring = wired(*says(ANSWER, pieces=2), signals=Mute(), wait_seconds=0.01)
    started = await wiring.turns.begin(AUTHOR, agent_id=AGENT, text="What is a robinaut?")

    with caplog.at_level(logging.WARNING):
        watcher, seen = watching(wiring, started.run)
        await asyncio.wait_for(watcher, timeout=BOUND)

    readable(seen, started.run)
    assert is_the_end(seen, RunState.FINISHED)
    assert "falls back to reading" in caplog.text


@asyncio_test
async def test_a_watcher_says_once_that_the_signals_are_failing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A listener that is down stays down, and every watcher of every run meets
    # it every wait. One line per stream is what says how many are affected;
    # one per wait would be the same fact thousands of times over, in the one
    # place a deployment is looked at. So the rest are DEBUG.
    held = Gate()
    signals = Mute()
    wiring = wired(TextDelta(text="Half of an"), held, signals=signals, wait_seconds=0.01)
    run = await begun(wiring)
    submitted(wiring, run)
    await held.reached.wait()

    with caplog.at_level(logging.DEBUG):
        watcher, seen = watching(wiring, run)
        while signals.asked < ASKED:
            await asyncio.sleep(0.005)
        watcher.cancel()
        with pytest.raises(asyncio.CancelledError):
            await watcher

    said = [record for record in caplog.records if "falls back to reading" in record.getMessage()]
    # Every attempt is accounted for, and exactly one of them was loud.
    assert signals.asked >= ASKED
    assert len(said) == signals.asked
    assert said[0].levelno == logging.WARNING
    assert {record.levelno for record in said[1:]} == {logging.DEBUG}
    held.open()
    await settled(wiring, run)


# --- cancelling, and a process that stops --------------------------------------


@asyncio_test
async def test_cancelling_reaches_the_work_and_the_watcher_is_told() -> None:
    held = Gate()
    wiring = wired(TextDelta(text="Half of an"), held)
    started = await wiring.turns.begin(AUTHOR, agent_id=AGENT, text="What is a robinaut?")
    run = started.run
    watcher, seen = watching(wiring, run)
    await held.reached.wait()
    await written(wiring.store, run.id, 3)

    asked = await wiring.turns.cancel(AUTHOR, run.id)

    assert asked.state is RunState.RUNNING
    await watcher
    assert is_the_end(seen, RunState.CANCELLED)
    readable(seen, run)
    assert (await wiring.store.run_by_id(run.id)).state is RunState.CANCELLED
    # The watcher is told the moment the end is stored; letting the engine go
    # is what the work does afterwards, so that is waited for separately.
    await settled(wiring, run)
    assert wiring.agent.released == 1


@asyncio_test
async def test_a_turn_whose_work_never_began_is_interrupted_by_the_shutdown() -> None:
    # The window: the work was submitted and the process began stopping before
    # the loop ever stepped it. Nothing of `execute` ran, so nothing of it
    # wrote the end -- the executor reports the run instead, while the stores
    # are still there.
    wiring = wired(*says(ANSWER))
    started = await wiring.turns.begin(AUTHOR, agent_id=AGENT, text="What is a robinaut?")

    wiring.turns.stopping()
    await wiring.executor.aclose(timeout=5.0)

    assert not wiring.agent.asked
    ended = await wiring.store.run_by_id(started.run.id)
    assert ended is not None
    assert ended.state is RunState.INTERRUPTED
    events = await stored_events(wiring.store, started.run.id)
    readable(events, ended)
    assert kinds(events) == ["RunStarted", "RunEnded"]
    assert wiring.turns.executing == frozenset()


@asyncio_test
async def test_a_shutdown_is_not_held_by_the_endings_of_runs_that_never_began(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Eight runs caught in the instant before their first step, a store that
    # takes half a second per write, and a fifth of a second to stop in. Each
    # ending is written under a shield, so nothing can interrupt one: a
    # shutdown that waited would take the store's time once per run, and a
    # process that had been carrying a hundred would never stop at all. The
    # bound wins instead -- and nothing is lost by its winning, because what
    # was being written is **left** to land rather than killed.
    store = Slow()
    wiring = wired(*says(ANSWER), store=store)
    runs = [await begun(wiring, text=f"What is a robinaut? ({n})") for n in range(MANY)]
    for run in runs:
        submitted(wiring, run)
    loop = asyncio.get_running_loop()

    wiring.turns.stopping()
    began = loop.time()
    with caplog.at_level(logging.WARNING):
        await wiring.executor.aclose(timeout=SHUTDOWN)

    assert loop.time() - began < SLOW
    assert "left to finish" in caplog.text
    assert not wiring.agent.asked
    # And every one of them is ended a moment later, by the write the close
    # walked away from. The store is still open here, which at a real
    # shutdown it would not be -- that is what the log line is for.
    async with asyncio.timeout(PATIENCE):
        for run in runs:
            while (await store.run_by_id(run.id)).state in ACTIVE_RUN_STATES:
                await asyncio.sleep(0.01)
    for run in runs:
        ended = await store.run_by_id(run.id)
        assert ended.state is RunState.INTERRUPTED
        readable(await stored_events(store, run.id), ended)
    assert wiring.turns.executing == frozenset()


@asyncio_test
async def test_a_process_that_stops_interrupts_its_runs_and_says_so() -> None:
    # Nobody cancelled this run: the process went away with it, which is a
    # different thing for its author and a different state in the record.
    held = Gate()
    wiring = wired(TextDelta(text="Half of an"), held)
    started = await wiring.turns.begin(AUTHOR, agent_id=AGENT, text="What is a robinaut?")
    run = started.run
    watcher, seen = watching(wiring, run)
    await held.reached.wait()
    await written(wiring.store, run.id, 3)

    wiring.turns.stopping()
    await wiring.executor.aclose(timeout=5.0)

    assert wiring.turns.is_stopping
    ended = await wiring.store.run_by_id(run.id)
    assert ended.state is RunState.INTERRUPTED
    assert ended.error is None
    await watcher
    assert is_the_end(seen, RunState.INTERRUPTED)
    readable(seen, run)
    # The answer in flight was never completed, so it is in no conversation.
    assert len(await wiring.store.messages_of(run.conversation_id)) == 1


# --- who may watch --------------------------------------------------------------


@asyncio_test
async def test_somebody_elses_run_is_answered_exactly_like_one_that_is_not_there() -> None:
    wiring = wired(*says(ANSWER))
    started = await wiring.turns.begin(AUTHOR, agent_id=AGENT, text="What is a robinaut?")
    await settled(wiring, started.run)

    with pytest.raises(RunNotFoundError):
        await anext(wiring.watch.events(SOMEBODY_ELSE, started.run.id))
    with pytest.raises(RunNotFoundError):
        await anext(wiring.watch.events(AUTHOR, uuid.uuid4()))


@asyncio_test
async def test_a_watcher_attaches_at_a_position_and_waits_for_a_bounded_time() -> None:
    wiring = wired(*says(ANSWER))
    started = await wiring.turns.begin(AUTHOR, agent_id=AGENT, text="What is a robinaut?")
    await settled(wiring, started.run)

    with pytest.raises(InvalidValueError):
        await anext(wiring.watch.events(AUTHOR, started.run.id, after=-1))
    with pytest.raises(InvalidValueError):
        Watch(store=wiring.store, signals=wiring.signals, wait_seconds=0)
    # And a wait longer than the silence it gives up after is refused rather
    # than quietly being the longer of the two: the silence is looked at after
    # each wait, so such a watcher would give up a whole wait late.
    with pytest.raises(InvalidValueError):
        Watch(store=wiring.store, signals=wiring.signals, wait_seconds=5.0, quiet_seconds=1.0)
