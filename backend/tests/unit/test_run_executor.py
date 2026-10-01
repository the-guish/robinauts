# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""``AsyncioRunExecutor``: what it starts, what it holds, and what it lets go.

The adapter that carries a run's work in the background. It knows nothing
about runs -- what is tested here is scheduling: that submitting answers at
once, that the work is held for as long as it runs and forgotten afterwards,
that what it raises is written down once and stops there, that cancelling
reaches it, and that shutting down is bounded.

**Nothing sleeps except where the bound itself is what is being tested**, and
what is waited for is always an event the work sets.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import logging
import uuid

import pytest

from aio import asyncio_test
from robinauts.legacy.adapters import CLOSED, AsyncioRunExecutor
from robinauts.legacy.domain import InvalidValueError, RunAlreadyActiveError

RUN = uuid.UUID("44444444-4444-4444-8444-444444444444")
OTHER = uuid.UUID("55555555-5555-4555-8555-555555555555")

BOUND = 0.05
"""How long a shutdown is given where the bound is the point of the test.

Real time, and the only real time in this module: it is the difference
between "aclose waited for work that will not stop" and "aclose returned".
"""

REPORTS = 5
"""How many never-began reports the close below has to get through."""

REPORT_BOUND = 0.2
"""The bound the whole of that close shares.

Long enough that ``REPORTS`` of them is a difference no machine can blur: a
close that gave each report a bound of its own would take a second, and one
that spends a single deadline on all of them takes this.
"""

ABANDONED = 10.0
"""A bound a second close must be seen **not** to be waiting under.

What the test with it asserts is that nothing was waited for, and the way to
assert that is a bound nothing could have waited under and come back -- not a
handful of milliseconds, which a loaded machine misses for reasons that have
nothing to do with an executor.
"""


class Work:
    """Work a test drives: it says when it began and waits to be let go."""

    def __init__(self) -> None:
        self.began = asyncio.Event()
        self.free = asyncio.Event()
        self.finished = False
        self.cancelled = False

    async def run(self) -> None:
        self.began.set()
        try:
            await self.free.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        self.finished = True


class Stubborn:
    """Work that swallows a cancellation, as an engine that will not let go.

    It is let go of by the test in the end, so the loop closes on nothing:
    work that really ignored every cancellation would hang the process, and
    what this is for is the bound, not the hanging.
    """

    def __init__(self) -> None:
        self.began = asyncio.Event()
        self.free = asyncio.Event()

    async def run(self) -> None:
        self.began.set()
        while not self.free.is_set():
            with contextlib.suppress(asyncio.CancelledError):
                await self.free.wait()


class Shielding:
    """A never-began report of the shape the application's is: it shields.

    What such a report writes is a run's ending, and it is written under
    ``asyncio.shield`` so that the cancellation a shutdown arrives with cannot
    leave the run ``running`` for ever (``robinauts.legacy.application.turns``).
    Nothing outside can stop it, then -- a close can only be told to stop
    waiting -- which is what makes the close's own deadline the only bound
    there is. ``minds_the_deadline`` is the difference between the
    application's report, which comes back at the deadline and leaves the
    write to land, and one that does not.
    """

    def __init__(self, *, minds_the_deadline: bool = False) -> None:
        self.write: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        """The ending being written. The test is what lets it land."""
        self.said = False
        self.minded = False
        self._minds = minds_the_deadline

    async def run(self, deadline: float) -> None:
        while True:
            try:
                if self._minds:
                    async with asyncio.timeout_at(deadline):
                        await asyncio.shield(self.write)
                else:
                    await asyncio.shield(self.write)
                break
            except asyncio.CancelledError:  # pragma: no cover -- nothing cancels it
                if self.write.done():
                    raise
            except TimeoutError:
                # The close's bound: the write is left to finish on its own,
                # and this comes back rather than holding the process open.
                self.minded = True
                return
        self.said = True


async def gone(executor: AsyncioRunExecutor, run_id: uuid.UUID) -> None:
    """Wait until that run's work has finished, without touching the task."""
    while run_id in executor.running():
        await asyncio.sleep(0)


# --- submitting ---------------------------------------------------------------


@asyncio_test
async def test_submitting_answers_at_once_and_the_work_runs_afterwards() -> None:
    executor = AsyncioRunExecutor()
    work = Work()

    executor.submit(RUN, work.run)

    # Not a step of it has run: submitting waits for nothing.
    assert not work.began.is_set()
    assert executor.running() == frozenset({RUN})
    work.free.set()
    await work.began.wait()
    await gone(executor, RUN)
    assert work.finished


@asyncio_test
async def test_work_that_has_finished_is_forgotten() -> None:
    executor = AsyncioRunExecutor()
    work = Work()
    work.free.set()

    executor.submit(RUN, work.run)
    await gone(executor, RUN)

    assert executor.running() == frozenset()
    # And the run may be answered again, which a registry that kept it could
    # not allow.
    again = Work()
    again.free.set()
    executor.submit(RUN, again.run)
    await gone(executor, RUN)
    assert again.finished


@asyncio_test
async def test_one_run_is_submitted_once_while_its_work_is_going() -> None:
    executor = AsyncioRunExecutor()
    work = Work()
    executor.submit(RUN, work.run)

    with pytest.raises(RunAlreadyActiveError):
        executor.submit(RUN, Work().run)

    work.free.set()
    await gone(executor, RUN)


@asyncio_test
async def test_what_is_submitted_is_something_to_call_and_a_run() -> None:
    executor = AsyncioRunExecutor()

    with pytest.raises(InvalidValueError):
        executor.submit(RUN, "the work")  # type: ignore[arg-type]
    with pytest.raises(InvalidValueError):
        executor.submit("a run", Work().run)  # type: ignore[arg-type]

    assert executor.running() == frozenset()


# --- what work raises ---------------------------------------------------------


@asyncio_test
async def test_work_that_raises_is_logged_once_and_stops_there(
    caplog: pytest.LogCaptureFixture,
) -> None:
    executor = AsyncioRunExecutor()
    unretrieved: list[object] = []
    asyncio.get_running_loop().set_exception_handler(
        lambda loop, context: unretrieved.append(context)
    )

    async def failing() -> None:
        raise RuntimeError("line one\nline two " + "x" * 5_000)

    with caplog.at_level(logging.ERROR):
        executor.submit(RUN, failing)
        await gone(executor, RUN)

    said = [record for record in caplog.records if str(RUN) in record.getMessage()]
    assert len(said) == 1
    # Escaped and bounded, like everything written down that somebody else
    # chose the shape of, and with the frames beside it.
    line = said[0].getMessage()
    assert "\n" not in line
    assert "RuntimeError" in line
    assert "line one" in line
    assert len(line) < 2_000
    assert "test_run_executor.py:" in line
    # The executor is not the run: it goes on executing.
    work = Work()
    work.free.set()
    executor.submit(OTHER, work.run)
    await gone(executor, OTHER)
    assert work.finished
    assert executor.running() == frozenset()
    # Nothing was left for the loop to complain about. A task's unretrieved
    # exception is reported by its `__del__`, so the tasks are collected and
    # the loop given a turn before this is believed.
    del work
    gc.collect()
    await asyncio.sleep(0)
    assert unretrieved == []


# --- cancelling ---------------------------------------------------------------


@asyncio_test
async def test_cancelling_reaches_the_work_and_says_that_it_did() -> None:
    executor = AsyncioRunExecutor()
    work = Work()
    executor.submit(RUN, work.run)
    await work.began.wait()

    assert executor.cancel(RUN) is True

    await gone(executor, RUN)
    assert work.cancelled
    assert not work.finished


@asyncio_test
async def test_cancelling_work_this_executor_has_none_of_is_no() -> None:
    executor = AsyncioRunExecutor()
    work = Work()
    work.free.set()
    executor.submit(RUN, work.run)
    await gone(executor, RUN)

    assert executor.cancel(RUN) is False
    assert executor.cancel(OTHER) is False


# --- shutting down ------------------------------------------------------------


@asyncio_test
async def test_closing_cancels_what_is_left_and_waits_for_it() -> None:
    executor = AsyncioRunExecutor()
    first, second = Work(), Work()
    executor.submit(RUN, first.run)
    executor.submit(OTHER, second.run)
    await first.began.wait()
    await second.began.wait()

    await executor.aclose(timeout=5.0)

    assert first.cancelled
    assert second.cancelled
    assert executor.running() == frozenset()


@asyncio_test
async def test_closing_is_bounded_by_work_that_will_not_stop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    executor = AsyncioRunExecutor()
    stubborn = Stubborn()
    executor.submit(RUN, stubborn.run)
    await stubborn.began.wait()

    with caplog.at_level(logging.WARNING):
        await executor.aclose(timeout=BOUND)

    # It came back, and said what it gave up on.
    assert "abandoned" in caplog.text
    stubborn.free.set()
    await gone(executor, RUN)


@asyncio_test
async def test_closing_again_waits_for_nothing_it_already_gave_up_on() -> None:
    executor = AsyncioRunExecutor()
    stubborn = Stubborn()
    executor.submit(RUN, stubborn.run)
    await stubborn.began.wait()
    loop = asyncio.get_running_loop()
    await executor.aclose(timeout=BOUND)

    began = loop.time()
    await executor.aclose(timeout=ABANDONED)

    # The second close does not sit through a bound again for work that was
    # abandoned by the first: a shutdown is not paid for twice. The work is
    # still going, so a close that waited for it would have waited the whole
    # of `ABANDONED` -- which is what is asserted here, rather than any
    # promptness of the loop.
    assert loop.time() - began < ABANDONED / 2
    stubborn.free.set()
    await gone(executor, RUN)


# --- work that never began ----------------------------------------------------


@asyncio_test
async def test_work_cancelled_before_it_began_is_reported() -> None:
    # The instant between submitting and the first step: the work ran no line,
    # so nothing it would have done has happened -- and whoever submitted it
    # believes the run is being answered.
    executor = AsyncioRunExecutor()
    work = Work()
    told: list[str] = []

    async def instead(deadline: float) -> None:
        told.append("given up on")

    executor.submit(RUN, work.run, never_began=instead)
    await executor.aclose(timeout=5.0)

    assert not work.began.is_set()
    assert told == ["given up on"]
    assert executor.running() == frozenset()


@asyncio_test
async def test_work_that_began_is_not_reported_as_never_having_begun() -> None:
    executor = AsyncioRunExecutor()
    work = Work()
    told: list[str] = []

    async def instead(deadline: float) -> None:  # pragma: no cover -- it is not called
        told.append("given up on")

    executor.submit(RUN, work.run, never_began=instead)
    await work.began.wait()

    await executor.aclose(timeout=5.0)

    # It was cancelled inside its own work, which is what ends its own run.
    assert work.cancelled
    assert told == []


@asyncio_test
async def test_a_report_that_fails_is_logged_and_the_close_goes_on(
    caplog: pytest.LogCaptureFixture,
) -> None:
    executor = AsyncioRunExecutor()
    told: list[str] = []

    async def failing(deadline: float) -> None:
        raise RuntimeError("the store went away")

    async def instead(deadline: float) -> None:
        told.append("given up on")

    executor.submit(RUN, Work().run, never_began=failing)
    executor.submit(OTHER, Work().run, never_began=instead)

    with caplog.at_level(logging.ERROR):
        await executor.aclose(timeout=5.0)

    assert told == ["given up on"]
    assert str(RUN) in caplog.text


@asyncio_test
async def test_reports_that_hang_share_the_one_bound_of_the_close(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # `timeout=` is how long the whole close may take, not how long each piece
    # of it may: a store that has stopped answering would otherwise cost one
    # bound per run that never began, and a process carrying a hundred of them
    # would not stop at all. These are of the shape the application's are --
    # the write is shielded, so **nothing can interrupt one** -- and what has
    # not come back when the deadline passes is left running, with a line in
    # the log, rather than waited for.
    executor = AsyncioRunExecutor()
    loop = asyncio.get_running_loop()
    reports = [Shielding() for _ in range(REPORTS)]
    for n, report in enumerate(reports):
        executor.submit(uuid.UUID(int=n, version=4), Work().run, never_began=report.run)

    began = loop.time()
    with caplog.at_level(logging.WARNING):
        await executor.aclose(timeout=REPORT_BOUND)

    assert loop.time() - began < REPORTS * REPORT_BOUND / 2
    assert "left running" in caplog.text
    assert executor.running() == frozenset()
    # Nothing was killed by the close walking away: the writes land, and the
    # reports finish saying what they were saying.
    for report in reports:
        report.write.set_result(None)
    while not all(report.said for report in reports):
        await asyncio.sleep(0)


@asyncio_test
async def test_a_report_that_comes_back_inside_the_bound_is_not_given_up_on(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A report that minds the deadline it was told -- which is what the
    # application's does: it leaves its shielded write to land and says so --
    # is a report the close waited for and got. Nothing was given up on, so
    # nothing here says anything was: the line is for a report that did not
    # come back, and a shutdown whose log said otherwise would have somebody
    # looking for runs that were ended perfectly well.
    executor = AsyncioRunExecutor()
    report = Shielding(minds_the_deadline=True)
    executor.submit(RUN, Work().run, never_began=report.run)

    with caplog.at_level(logging.WARNING):
        await executor.aclose(timeout=BOUND)

    assert report.minded
    assert [record for record in caplog.records if record.levelno >= logging.WARNING] == []
    report.write.set_result(None)


@asyncio_test
async def test_a_shutdown_is_bounded_in_seconds() -> None:
    executor = AsyncioRunExecutor()

    for bound in (-1.0, float("inf"), float("nan"), True, "soon", None):
        with pytest.raises(InvalidValueError):
            await executor.aclose(timeout=bound)  # type: ignore[arg-type]

    # And asking wrongly closed nothing: a mistake in the caller is not a
    # process that has begun stopping.
    work = Work()
    work.free.set()
    executor.submit(RUN, work.run)
    await gone(executor, RUN)
    assert work.finished
    await executor.aclose(timeout=BOUND)


@asyncio_test
async def test_what_to_run_instead_is_something_to_call() -> None:
    executor = AsyncioRunExecutor()

    with pytest.raises(InvalidValueError):
        executor.submit(RUN, Work().run, never_began="end it")  # type: ignore[arg-type]

    assert executor.running() == frozenset()


@asyncio_test
async def test_a_closed_executor_starts_nothing_and_closes_again_quietly() -> None:
    executor = AsyncioRunExecutor()
    await executor.aclose(timeout=BOUND)

    with pytest.raises(InvalidValueError) as refused:
        executor.submit(RUN, Work().run)

    assert CLOSED in str(refused.value)
    await executor.aclose(timeout=BOUND)
    assert executor.running() == frozenset()
