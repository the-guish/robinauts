# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Runs in the background, as asyncio tasks on the loop that serves requests.

The ``RunExecutor`` of this version (``docs/specs/backend.md``, "Background
work"): there is no worker, no queue and no scheduler, because a run is
I/O-bound and the loop that serves requests is where it waits. FastAPI's
``BackgroundTasks`` is deliberately not used -- it is tied to a request, and a
run outlives the request that started it (``docs/specs/runs.md``).

What this has to get right is small and easy to get wrong:

- **a strong reference to every task.** A task nobody holds may be collected
  while it runs, and the run it was answering would simply stop, with nothing
  written anywhere. The registry is that reference, and the done callback is
  what takes it out again -- keyed on the task, so that a callback arriving
  after another submission cannot remove somebody else's;
- **every exception retrieved, and said once.** Work that raises must not
  reach the loop's "exception was never retrieved" at some later moment, in
  nobody's frame; it is caught where it is run and written to the log through
  ``domain.chain`` / ``domain.where``, escaped and bounded like everything
  written down that somebody else chose the shape of. It stops there: one
  failed run is not an executor that stops executing;
- **work that never began is reported.** There is an instant between creating
  a task and its first step, and a shutdown landing in it cancels work that
  never ran a line: no ending was written, and the caller believes the run is
  being answered. So every submission may name something to run **instead**
  if that happens (``never_began``), and ``aclose`` awaits it before it
  returns -- while the stores are still open, because what it does is write
  the run's end into one;
- **a bounded shutdown, and one bound for the whole of it.** ``aclose``
  cancels what is left and waits under a bound. What the work then does is the
  application's -- a run writes its ending under a shield, so the bound must be
  long enough for that -- and what has not finished when the bound passes is
  abandoned with a line in the log, rather than holding a process open. The
  bound is a **deadline**, shared by the wait and by every report that follows
  it: a close that gave each of them a bound of its own would be a shutdown
  that takes the bound times however many runs were in flight, which is a
  process that will not stop. Every report is told that deadline, because what
  it writes may be shielded and a shielded write is not something an executor
  can interrupt -- it can only be left, in a task of its own, with a line in
  the log. A second ``aclose`` waits for none of it again.

It decides nothing about runs. Which run may be executed, what a cancellation
means, and what is written when work stops are all above this
(``robinauts.legacy.application.turns``).
"""

from __future__ import annotations

import asyncio
import functools
import logging
import math
import uuid
from dataclasses import dataclass, field

from robinauts.legacy.domain import (
    InvalidValueError,
    RunAlreadyActiveError,
    chain,
    checked_uuid,
    describe,
    where,
)
from robinauts.legacy.ports import RunExecutor, RunReport, RunWork

_log = logging.getLogger(__name__)

DEFAULT_SHUTDOWN_SECONDS = 30.0
"""How long ``aclose`` waits, when the caller says nothing at all.

What a close needs is long enough for what a cancelled run owes -- its ending
is written under a shield, each attempt bounded and tried a few times -- and
those numbers are the application's, which an adapter may not read
(``docs/layout.md``). So **the deployment does not use this**: the composition
root adds up the application's own ending budget and hands the result to
``aclose`` (``robinauts.legacy.app.SHUTDOWN_SECONDS``). This is what is left for a
caller with nothing to say about it, a test among them: the same order of
magnitude, and short enough that a process which cannot reach its database
still stops -- what is left is abandoned, and the runs it was answering are
ended by the sweep of the next start-up.
"""

CLOSED = "this executor has been closed; the process is stopping and starts no more work"
"""What a submission during shutdown is refused with."""


@dataclass(slots=True)
class _Carried:
    """One run's work: the task, and whether it ever got a step."""

    task: asyncio.Task[None]
    never_began: RunReport | None = None
    began: bool = field(default=False)


class AsyncioRunExecutor(RunExecutor):
    """Every run this process is answering, as one task each."""

    def __init__(self) -> None:
        self._carried: dict[uuid.UUID, _Carried] = {}
        """The live work, keyed by run, and the reference that keeps it alive."""
        self._closed = False
        self._abandoned: set[asyncio.Task[None]] = set()
        """Work a close gave up on: never waited for a second time."""
        self._saying: set[asyncio.Task[None]] = set()
        """Reports a close left running, held so that none is collected.

        A report that is still going when the deadline passes is let be rather
        than killed -- it may be half way through writing a run's ending --
        and something has to hold it until it finishes. It takes itself out.
        """
        self._to_report: dict[uuid.UUID, RunReport] = {}
        """What to run for work that was cancelled before it ever began.

        Filled by the done callback, which is where "cancelled, and never got
        a step" can be seen, and drained by ``aclose``, which is the one place
        here that may await anything.
        """

    def submit(
        self, run_id: uuid.UUID, work: RunWork, *, never_began: RunReport | None = None
    ) -> None:
        """Run ``work`` on this loop, from now, without waiting for any of it."""
        checked_uuid(run_id, "a run's id")
        if not callable(work):
            raise InvalidValueError(f"work is something to call, not {describe(work)}")
        if never_began is not None and not callable(never_began):
            raise InvalidValueError(
                f"what to run instead is something to call, not {describe(never_began)}"
            )
        if self._closed:
            raise InvalidValueError(CLOSED)
        standing = self._carried.get(run_id)
        if standing is not None and not standing.task.done():
            raise RunAlreadyActiveError(f"run {run_id} is already being executed by this process")
        # A task that is done but whose callback has not run yet is not work:
        # it is an entry on its way out, and it is replaced rather than
        # standing in the way of a run this process may legitimately answer
        # again.
        carried = _Carried(
            task=asyncio.get_running_loop().create_task(
                self._working(run_id, work), name=f"robinauts-run-{run_id}"
            ),
            never_began=never_began,
        )
        # The task is made before the entry exists, so `_working` reaches for
        # the entry rather than being handed it: its first line says that the
        # work began, and it cannot run before this returns.
        self._carried[run_id] = carried
        carried.task.add_done_callback(functools.partial(self._done, run_id, carried))

    def cancel(self, run_id: uuid.UUID) -> bool:
        """Ask that run's work to stop; whether there was any to ask.

        It does **not** answer for work that was submitted and has not had a
        step: cancelling that writes no ending anywhere, which is why the
        application only cancels work it knows has begun and ends every other
        run in the store instead (``docs/specs/runs.md``).
        """
        checked_uuid(run_id, "a run's id")
        carried = self._carried.get(run_id)
        if carried is None or carried.task.done():
            return False
        return carried.task.cancel()

    def running(self) -> frozenset[uuid.UUID]:
        """The runs whose work has not finished."""
        return frozenset(
            run_id for run_id, carried in self._carried.items() if not carried.task.done()
        )

    async def aclose(self, *, timeout: float = DEFAULT_SHUTDOWN_SECONDS) -> None:
        """Cancel everything left and wait for it, once, under one bound.

        ``asyncio.wait`` rather than ``gather``: it bounds the wait without
        cancelling **this** task in turn, and it behaves the same whether or
        not whoever is shutting the process down has been cancelled itself.
        What is still going when the bound passes keeps its done callback, so
        its result is read whenever it finishes and the loop says nothing
        about it on its way out -- and it is remembered as abandoned, so a
        second close does not wait the bound out again for it.

        Then, and only then, whatever was cancelled **before it ever began**
        is reported. It is last because it is the one thing here that talks to
        the outside world, and it runs while the process still holds
        everything it needs to.

        **``timeout`` is how long the whole close may take**, not how long
        each piece of it may: it becomes a deadline, and the wait above and
        every report below spend what is left of it. Whoever is stopping the
        process asked for a bound, and a bound multiplied by the number of
        runs in flight is not one. ``InvalidValueError`` if it is not a number
        of seconds: that is a mistake in the caller, and a shutdown that ran
        for ever on one would be the worst way to find out.
        """
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, int | float)
            or not math.isfinite(timeout)
            or timeout < 0
        ):
            raise InvalidValueError(f"a shutdown is bounded in seconds, not {describe(timeout)}")
        self._closed = True
        deadline = asyncio.get_running_loop().time() + timeout
        left = {
            carried.task
            for carried in self._carried.values()
            if not carried.task.done() and carried.task not in self._abandoned
        }
        for task in left:
            task.cancel()
        if left:
            _, still = await asyncio.wait(left, timeout=_until(deadline))
            if still:
                self._abandoned |= still
                _log.warning(
                    "%d run(s) had not stopped %ss after the process began shutting down;"
                    " they were abandoned, and what they were answering is ended by the"
                    " start-up sweep of the next restart",
                    len(still),
                    timeout,
                )
        await self._reported(deadline)
        # Whatever finished has been waited for; whatever has not keeps its
        # callback, and neither is this executor's any more.
        self._carried = {
            run_id: carried
            for run_id, carried in self._carried.items()
            if carried.task in self._abandoned
        }

    async def _reported(self, deadline: float) -> None:
        """Say that work which never began was given up on, once each.

        The window this is for is one instant wide -- between a submission and
        its first step -- and a shutdown landing in it would otherwise leave a
        run that nothing ever executed and nobody ever ended. The done
        callback sees it happen and leaves the report here; this is where it
        is run, because this is the one method that may await, and it is run
        **while the process still holds its stores**.

        ``deadline`` is the close's own and every report is told it
        (``robinauts.legacy.ports.RunReport``), so all of them together are bounded
        by what the caller asked for rather than each of them being.

        **And each runs in a task of its own, which is what makes the bound
        true.** What a report writes may be shielded -- that is how a run's
        ending survives the cancellation that arrives with a shutdown -- and
        nothing outside a shield can interrupt it, so a report cannot be
        stopped, only *left*. It is started, waited for under what is left of
        the deadline, and if it is still going then it is said so and let be:
        it holds its own reference, finishes or fails on its own and writes
        that down, and this close does not wait for it. A report that respects
        the deadline it was given never gets that far, which is what the
        application's does.
        """
        while self._to_report:
            run_id, report = self._to_report.popitem()
            saying = asyncio.get_running_loop().create_task(
                self._saying_so(run_id, report, deadline), name=f"robinauts-report-{run_id}"
            )
            # Held until it is done, like every other task here: a report
            # nobody references may be collected half way through a write.
            self._saying.add(saying)
            saying.add_done_callback(self._saying.discard)
            await asyncio.wait({saying}, timeout=_until(deadline))
            if not saying.done():
                _log.warning(
                    "run %s was given up on before its work began, and saying so had not"
                    " come back when the shutdown's bound passed; it was left running, and"
                    " the run is ended by the start-up sweep of the next restart if it"
                    " never lands",
                    run_id,
                )

    async def _saying_so(self, run_id: uuid.UUID, report: RunReport, deadline: float) -> None:
        """Run one report, and let nothing out of it but a cancellation.

        Nobody waits on this past the close's deadline, so what it raised must
        be written down here or not at all -- and it must be **retrieved**,
        which is the same reason ``_working`` catches what work raises.
        """
        try:
            await report(deadline)
        except asyncio.CancelledError:
            raise
        except Exception as failure:  # noqa: BLE001 - shutdown reports to the log
            _log.error(
                "run %s was given up on before its work began, and saying so failed: %s",
                run_id,
                chain(failure),
            )

    async def _working(self, run_id: uuid.UUID, work: RunWork) -> None:
        """Run the work of one run, and let nothing out but a cancellation.

        **What it raised is written down here and goes no further.** Nobody is
        waiting on this task -- the request that started the run was answered
        long ago -- so an exception left in it would be a failure nobody ever
        heard of, reported by the loop at some later moment in nobody's frame.
        A cancellation is not a failure and is let through, which is also what
        tells ``aclose`` that the work really stopped.
        """
        carried = self._carried.get(run_id)
        if carried is not None:
            # The first thing, and before any await: from here a cancellation
            # lands inside the work, which ends its own run.
            carried.began = True
        try:
            await work()
        except asyncio.CancelledError:
            raise
        except Exception as failure:  # noqa: BLE001 - the last frame there is
            _log.error(
                "the work of run %s failed: %s at %s", run_id, chain(failure), where(failure)
            )

    def _done(self, run_id: uuid.UUID, carried: _Carried, task: asyncio.Task[None]) -> None:
        """Forget that task and read what it left, so nothing is reported twice.

        Keyed on the entry as well as the run: a run submitted again after
        this one finished has work of its own, and a callback arriving late
        must not take it out of the registry.

        **Work cancelled before it ever began leaves a report behind**, for
        ``aclose`` to run. This callback cannot await anything, so it cannot
        make that report itself; what it can do is see that it is owed, which
        nothing else can.
        """
        if task.cancelled() and not carried.began and carried.never_began is not None:
            self._to_report[run_id] = carried.never_began
            carried.never_began = None
        if self._carried.get(run_id) is carried:
            del self._carried[run_id]
        self._abandoned.discard(task)
        if not task.cancelled():
            # Retrieved, never logged: `_working` has already said whatever
            # there was to say, and anything here is what escaped it.
            failure = task.exception()
            if failure is not None:  # pragma: no cover -- `_working` catches
                _log.error("the work of run %s ended badly: %s", run_id, chain(failure))


def _until(deadline: float) -> float:
    """What is left of a close's bound, and never less than nothing."""
    return max(deadline - asyncio.get_running_loop().time(), 0.0)
