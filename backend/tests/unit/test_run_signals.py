# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""``MemoryRunSignals``: waking watchers, and what it remembers afterwards.

The port carries no data (``robinauts.legacy.ports.RunSignals``), so what there is to
test is exactly four things: that an announcement wakes what is waiting; that
a position already passed is an answer rather than a wait -- **the end
included**, because a watcher attaching to a run that finished a moment ago
asks about a position and must not be made to sit out the whole bound for an
answer that is known; that a wait nobody answers comes back by itself, which
is what turns a lost signal into a poll; and that the memory which makes the
second of those possible is **bounded**, in how many runs it holds and in how
long it holds them, so that a deployment which has answered a great many turns
is holding a record for none of them -- and that keeping it bounded **costs
nothing**, because the sweep that does it rides on the announcement of every
delta of every answer.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from aio import asyncio_test
from robinauts.legacy.adapters import REMEMBERED_RUNS, MemoryRunSignals
from robinauts.legacy.domain import InvalidValueError

RUN = uuid.UUID("44444444-4444-4444-8444-444444444444")
OTHER = uuid.UUID("55555555-5555-4555-8555-555555555555")

BOUND = 0.05
"""The bound where the bound is the point: short, and really waited."""

LONG = 30.0
"""A bound no test of a signal that arrives should ever reach."""

ANNOUNCES = 25_000
"""Announcements enough that anything per-record in the sweep would show.

Past ``REMEMBERED_RUNS``, so the memory is full for most of them and
forgetting something is what every one of them does.
"""

FLAT = 1.0
"""How long all of those may take together. A ceiling, not a measurement.

A sweep that copied or walked what is remembered would be quadratic in it --
minutes for the run below, and milliseconds of blocked loop per token in a
process that had answered a few thousand turns. This is loose enough that a
busy machine passes it and far tighter than any such sweep can be.
"""

WATCHERS = 5_000
"""Watchers parked on quiet runs while another run is answering.

The shape a deployment is, and the one a sweep that stepped **over** the
records it may not forget would be undone by: a move per watcher per event.
"""

BUSY_ANNOUNCES = 2_000
"""One run's worth of deltas, announced while those watchers wait."""

FLAT_FACTOR = 3.0
"""How much dearer those may be with all those watchers than with none."""

NOTICEABLE = 0.05
"""The floor under that comparison: below this a ratio is measuring noise."""


async def announcing(signals: MemoryRunSignals, run_id: uuid.UUID, times: int) -> float:
    """How long it takes to announce that one run that many times, in seconds."""
    loop = asyncio.get_running_loop()
    began = loop.time()
    for seq in range(1, times + 1):
        await signals.announce(run_id, seq)
    return loop.time() - began


async def waiting(
    signals: MemoryRunSignals, run_id: uuid.UUID, after: int, *, timeout: float = LONG
) -> asyncio.Task[bool]:
    """A watcher waiting on that run, already parked in ``changed``."""
    task = asyncio.get_running_loop().create_task(signals.changed(run_id, after, timeout=timeout))
    while run_id not in signals.tracked:
        await asyncio.sleep(0)
    await asyncio.sleep(0)
    return task


# --- being woken --------------------------------------------------------------


@asyncio_test
async def test_a_watcher_is_woken_by_an_announcement_past_where_it_is() -> None:
    signals = MemoryRunSignals()
    watcher = await waiting(signals, RUN, 3)

    await signals.announce(RUN, 4)

    assert await watcher is True


@asyncio_test
async def test_every_watcher_of_one_run_is_woken() -> None:
    signals = MemoryRunSignals()
    watchers = [await waiting(signals, RUN, 0) for _ in range(5)]

    await signals.announce(RUN, 1)

    assert await asyncio.gather(*watchers) == [True] * 5


@asyncio_test
async def test_a_watcher_of_another_run_is_left_alone() -> None:
    signals = MemoryRunSignals()
    watcher = await waiting(signals, OTHER, 0, timeout=BOUND)

    await signals.announce(RUN, 7)

    assert await watcher is False


@asyncio_test
async def test_the_end_of_a_run_wakes_a_watcher_wherever_it_is() -> None:
    signals = MemoryRunSignals()
    watcher = await waiting(signals, RUN, 9)

    # The run ended at a position the watcher has already seen, which is what
    # a watcher that read everything and then waited looks like.
    await signals.announce(RUN, 9, ended=True)

    assert await watcher is True


@asyncio_test
async def test_a_position_already_passed_is_an_answer_and_not_a_wait() -> None:
    signals = MemoryRunSignals()
    await signals.announce(RUN, 4)

    # Nothing is waited for: the answer is known, which is what stops a
    # watcher that was busy with what it had from sleeping through what was
    # announced while it was away.
    assert await signals.changed(RUN, 3, timeout=BOUND) is True
    assert await signals.changed(RUN, 4, timeout=BOUND) is False


# --- a signal that never comes ------------------------------------------------


@asyncio_test
async def test_a_wait_nobody_answers_comes_back_by_itself() -> None:
    signals = MemoryRunSignals()

    assert await signals.changed(RUN, 0, timeout=BOUND) is False


@asyncio_test
async def test_a_watcher_that_goes_away_stops_waiting() -> None:
    signals = MemoryRunSignals()
    watcher = await waiting(signals, RUN, 0)

    watcher.cancel()

    with pytest.raises(asyncio.CancelledError):
        await watcher
    assert signals.tracked == frozenset()


# --- what is kept -------------------------------------------------------------


@asyncio_test
async def test_a_run_that_has_ended_is_still_answered_about() -> None:
    # The case a watcher meets constantly: the run ended while it was busy
    # with what it had, and it comes back and asks about its position. That
    # question has an answer, and waiting the bound out for it would be a
    # stream that arrives seconds late for nothing.
    signals = MemoryRunSignals()

    await signals.announce(RUN, 1)
    await signals.announce(RUN, 2, ended=True)

    assert await signals.changed(RUN, 0, timeout=BOUND) is True
    assert await signals.changed(RUN, 2, timeout=BOUND) is True
    assert signals.tracked == frozenset({RUN})


@asyncio_test
async def test_the_memory_holds_a_bounded_number_of_runs() -> None:
    signals = MemoryRunSignals(remember_most=3)
    runs = [uuid.UUID(f"00000000-0000-4000-8000-{n:012d}") for n in range(6)]

    for run_id in runs:
        await signals.announce(run_id, 1, ended=True)

    kept = signals.tracked
    assert len(kept) <= 3
    # The newest are the ones worth answering about.
    assert runs[-1] in kept
    assert runs[0] not in kept


@asyncio_test
async def test_announcing_costs_the_same_however_much_is_remembered() -> None:
    # The sweep rides on the announcements, which are on the path of every
    # delta of every answer: it must look at the head of what is remembered
    # and at nothing else. Twenty-five thousand distinct runs at the real
    # bounds, which is where a sweep proportional to the memory stops being a
    # cost and becomes a broken deployment.
    signals = MemoryRunSignals()
    began = asyncio.get_running_loop().time()

    for n in range(ANNOUNCES):
        await signals.announce(uuid.UUID(int=n, version=4), 1, ended=True)

    assert asyncio.get_running_loop().time() - began < FLAT
    # And the ceiling held throughout: that is what it was forgetting for.
    assert len(signals.tracked) == REMEMBERED_RUNS


@asyncio_test
async def test_the_watchers_a_process_has_are_not_in_the_way_of_announcements() -> None:
    # The other shape, and the one a deployment actually is: a great many
    # watchers parked on runs that are saying nothing, and one run producing
    # an answer. The records they are waiting on may not be forgotten -- so
    # they are held apart from the order the sweep reads, rather than stepped
    # over in it, because stepping over them is a move per watcher per event.
    signals = MemoryRunSignals()
    alone = await announcing(signals, RUN, BUSY_ANNOUNCES)
    watchers = [
        asyncio.get_running_loop().create_task(
            signals.changed(uuid.UUID(int=n, version=4), 0, timeout=LONG)
        )
        for n in range(WATCHERS)
    ]
    while len(signals.tracked) < WATCHERS + 1:
        await asyncio.sleep(0)

    watched = await announcing(signals, RUN, BUSY_ANNOUNCES)

    # The same announcement, whatever this process is carrying beside it.
    assert watched < max(FLAT_FACTOR * alone, NOTICEABLE)
    for watcher in watchers:
        watcher.cancel()
    await asyncio.gather(*watchers, return_exceptions=True)
    # And nothing was ever said about the runs they were waiting on, so what
    # is left is the one that was answering.
    assert signals.tracked == frozenset({RUN})


@asyncio_test
async def test_what_was_said_is_forgotten_after_a_while() -> None:
    signals = MemoryRunSignals(remember_seconds=0.01)
    await signals.announce(RUN, 2, ended=True)

    await asyncio.sleep(0.02)
    # Swept as the next thing comes through: no task of its own for a
    # dictionary.
    await signals.announce(OTHER, 1)

    assert RUN not in signals.tracked
    # A watcher of a run nothing is remembered about falls back to the poll,
    # which is what happens to a signal that was never sent.
    assert await signals.changed(RUN, 0, timeout=BOUND) is False


@asyncio_test
async def test_a_run_somebody_is_waiting_on_is_never_forgotten() -> None:
    signals = MemoryRunSignals(remember_most=1, remember_seconds=0.01)
    watcher = await waiting(signals, RUN, 0)
    await asyncio.sleep(0.02)

    await signals.announce(OTHER, 1, ended=True)

    assert RUN in signals.tracked
    await signals.announce(RUN, 1)
    assert await watcher is True


@asyncio_test
async def test_a_run_nothing_was_said_about_is_forgotten_with_its_last_watcher() -> None:
    signals = MemoryRunSignals()

    assert await signals.changed(RUN, 0, timeout=BOUND) is False

    assert signals.tracked == frozenset()


# --- what it refuses ----------------------------------------------------------


@asyncio_test
async def test_a_memory_is_a_number_of_runs_and_a_length_of_time() -> None:
    for changes in ({"remember_most": 0}, {"remember_most": True}, {"remember_seconds": 0}):
        with pytest.raises(InvalidValueError):
            MemoryRunSignals(**changes)  # type: ignore[arg-type]


@asyncio_test
async def test_a_signal_is_about_a_run_at_a_position() -> None:
    signals = MemoryRunSignals()

    with pytest.raises(InvalidValueError):
        await signals.announce("a run", 1)  # type: ignore[arg-type]
    with pytest.raises(InvalidValueError):
        await signals.announce(RUN, -1)
    with pytest.raises(InvalidValueError):
        await signals.changed(RUN, -1, timeout=BOUND)
    with pytest.raises(InvalidValueError):
        await signals.changed(RUN, 0, timeout=0)
    assert signals.tracked == frozenset()
