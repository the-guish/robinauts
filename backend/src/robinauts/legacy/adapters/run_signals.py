# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Telling this process's watchers that a run has moved, in memory.

The ``RunSignals`` of this version: one process, so an announcement is a few
futures being resolved on the loop everything already runs on
(``docs/working-notes/poc-scope.md``). Over PostgreSQL ``LISTEN``/``NOTIFY``
it becomes a statement and a connection listening; nothing above the port
changes, which is what the port is for.

**It carries nothing and is believed about nothing.** What it keeps per run is
a number -- the furthest position anybody has announced -- and whether the run
was announced as over. A watcher woken by it goes and reads the store, and a
watcher that is not woken reads the store anyway when its bound passes, so the
worst a lost announcement can do is make a stream arrive a wait later.

**What is said is remembered for a while, the end included.** A watcher does
not ask "wake me when something happens"; it asks "is there anything past
this position", and that question has an answer the moment it is asked for
everything already announced. Forgetting a run as it ended would make the
common case -- attach to a run that finished a moment ago, ask about the
position you have -- wait out the whole bound for an answer that was known,
which is a stream that arrives seconds late for no reason. So a run's record
outlives it.

**And the memory is bounded**, because a process that has answered a million
turns must not hold a record for each of them: at most ``REMEMBERED_RUNS``
records **without watchers**, and none of them for longer than
``REMEMBERED_SECONDS`` since the last thing said about it. Both bounds are on
the records nobody is waiting on, which is the memory this keeps of its own
accord; a record with watchers is one the process is using. A record somebody
is waiting on is never forgotten -- waking them is what it is for -- and is
outside both bounds, because the number of watchers a process has is not
something it may quietly drop.

**The sweep costs nothing to run.** It is lazy, on the announcements and waits
that happen anyway, and it looks at the **head** of a dictionary held in the
order things were last said: it forgets while the oldest record is over a
bound, and stops at the first one that is not. Nothing is copied and nothing
is scanned -- an announcement is on the path of every delta of every answer,
and a sweep that walked ten thousand records would be milliseconds of blocked
loop per token. The records somebody is waiting on are held **apart** from
that order rather than skipped inside it, so that a process with five thousand
watchers pays nothing at all for them on the way through.

A watcher of a run that **has** been forgotten falls back to the bounded poll,
which is the same thing that happens to a signal that was never sent. A
``LISTEN``/``NOTIFY`` adapter keeps the same memory, per process, for the same
reason.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field

from robinauts.legacy.domain import InvalidValueError, checked_uuid, describe
from robinauts.legacy.ports import RunSignals

REMEMBERED_RUNS = 10_000
"""How many runs with nobody waiting on them are remembered at most.

The oldest are forgotten first. A ceiling on what a long-lived process holds,
not a working set: a deployment answering as fast as it can still has only its
live runs and the ones that ended in the last ``REMEMBERED_SECONDS`` inside
it. Records that **have** watchers are not counted here and are never
forgotten -- see the module docstring.
"""

REMEMBERED_SECONDS = 60.0
"""How long what was said about a run is remembered after the last word of it.

Long enough that a watcher attaching to a run that has just ended is answered
rather than made to wait, and short enough that nothing here is a record of
anything. A run whose end is older than this is a run whose events are simply
read from the store.
"""


@dataclass(slots=True)
class _Watched:
    """What is known about one run, and who is waiting to hear more.

    ``last`` is remembered rather than only delivered, because a watcher that
    was busy with what it had -- writing an event out to a browser -- must not
    then wait for an announcement that was made while it was away. It asks
    about a position, and a position that has already been passed is an answer
    and not a wait. ``said_at`` is when the last thing was said, which is what
    the memory is bounded by.
    """

    last: int = 0
    ended: bool = False
    said_at: float = 0.0
    waiters: set[asyncio.Future[None]] = field(default_factory=set)


class MemoryRunSignals(RunSignals):
    """A run's watchers, woken when the run's stream moves."""

    def __init__(
        self,
        *,
        remember_most: int = REMEMBERED_RUNS,
        remember_seconds: float = REMEMBERED_SECONDS,
    ) -> None:
        if isinstance(remember_most, bool) or not isinstance(remember_most, int):
            raise InvalidValueError(
                f"a memory holds a number of runs, not {describe(remember_most)}"
            )
        if remember_most < 1:
            raise InvalidValueError("a memory holds at least one run")
        if (
            isinstance(remember_seconds, bool)
            or not isinstance(remember_seconds, int | float)
            or remember_seconds <= 0
        ):
            raise InvalidValueError(
                f"a memory is bounded in seconds, not {describe(remember_seconds)}"
            )
        self._runs: OrderedDict[uuid.UUID, _Watched] = OrderedDict()
        """The records **nobody is waiting on**, oldest spoken about first.

        The memory this keeps of its own accord, and the only thing either
        bound is about. The sweep reads its head and nothing else.
        """
        self._watched: dict[uuid.UUID, _Watched] = {}
        """The records somebody **is** waiting on. Never forgotten, never swept.

        A separate structure rather than a flag, because the sweep must not so
        much as step over them: a process with five thousand watchers and one
        busy run would otherwise pay five thousand moves for every event of
        it, which is a blocked loop per token. A record crosses from one to
        the other when its first waiter arrives and back when its last leaves.
        """
        self._most = remember_most
        self._seconds = remember_seconds

    @property
    def tracked(self) -> frozenset[uuid.UUID]:
        """The runs anything is remembered about. Diagnostics, and a test.

        It is what the bound on the memory is read from: the runs in flight,
        the ones anybody is waiting on, and those that ended recently enough
        to still be worth answering about.
        """
        return frozenset(self._runs) | frozenset(self._watched)

    async def announce(self, run_id: uuid.UUID, seq: int, *, ended: bool = False) -> None:
        """Say that this run reaches ``seq``, and wake everyone waiting."""
        checked_uuid(run_id, "a run's id")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            raise InvalidValueError(f"a position is a whole number, not {describe(seq)}")
        watched = self._record(run_id)
        if watched is None:
            watched = _Watched()
            self._runs[run_id] = watched
        watched.last = max(watched.last, seq)
        watched.ended = watched.ended or ended
        watched.said_at = self._now()
        if run_id in self._runs:
            self._runs.move_to_end(run_id)
        for waiter in tuple(watched.waiters):
            if not waiter.done():
                waiter.set_result(None)
        self._swept()

    async def changed(self, run_id: uuid.UUID, after: int, *, timeout: float) -> bool:
        """Wait, under ``timeout``, until this run has something past ``after``."""
        checked_uuid(run_id, "a run's id")
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise InvalidValueError(
                f"a position to carry on from is a whole number, not {describe(after)}"
            )
        if isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout <= 0:
            raise InvalidValueError(f"a wait is bounded in seconds, not {describe(timeout)}")
        watched = self._record(run_id)
        if watched is not None and (watched.last > after or watched.ended):
            return True
        if watched is None:
            watched = _Watched(said_at=self._now())
            self._watched[run_id] = watched
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        watched.waiters.add(waiter)
        if len(watched.waiters) == 1:
            # The first waiter takes the record out of the forgettable order
            # altogether: while somebody is on it, it is neither forgotten nor
            # looked at.
            self._runs.pop(run_id, None)
            self._watched[run_id] = watched
        try:
            await asyncio.wait_for(waiter, timeout)
            return True
        except TimeoutError:
            # Nothing was said. The watcher reads anyway, which is what makes
            # a lost signal cost a wait rather than a stream.
            return False
        finally:
            watched.waiters.discard(waiter)
            if not watched.waiters and self._watched.get(run_id) is watched:
                del self._watched[run_id]
                if watched.last or watched.ended:
                    # Back among the forgettable, at the end of the order: it
                    # was being used until now, and the order is recency.
                    self._runs[run_id] = watched
                # Otherwise nothing was ever said about this run, and what is
                # left is the record a watcher of somebody else's run made by
                # waiting. There is nothing in it to remember.
            self._swept()

    def _record(self, run_id: uuid.UUID) -> _Watched | None:
        """What is remembered about that run, whichever side it is being kept on."""
        watched = self._watched.get(run_id)
        return watched if watched is not None else self._runs.get(run_id)

    @staticmethod
    def _now() -> float:
        """The loop's own monotonic reading: intervals, never times."""
        return asyncio.get_running_loop().time()

    def _swept(self) -> None:
        """Forget what the memory no longer holds, oldest first and cheaply.

        Lazily, on the calls that already happen, because a sweeper of its own
        would be a task to start and stop for a dictionary -- and **without
        looking at anything but the head**: this runs on the path of every
        delta of every answer, so a sweep proportional to what is remembered
        would be milliseconds of blocked loop per token, which is what an
        announcement must never cost. Every turn below either forgets a record
        or is the last one.

        **Both bounds are on the records nobody is waiting on**, which are the
        only ones here: a record somebody is waiting on is kept apart
        (``self._watched``), because waking a watcher is what the memory is
        for, and it is not so much as stepped over while it waits. It comes
        back at the end of this order when its last waiter leaves, so a record
        that was watched is forgotten by the count rather than by its age: the
        age is what keeps a quiet memory small, and the count is the ceiling
        that holds either way.
        """
        now = self._now()
        while self._runs:
            run_id, watched = next(iter(self._runs.items()))
            if len(self._runs) <= self._most and now - watched.said_at < self._seconds:
                return
            del self._runs[run_id]
