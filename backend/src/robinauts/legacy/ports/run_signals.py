# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""How a watcher hears that a run has stored something new, without asking.

A run's events are **stored** as they are produced and read back from the
store by whoever is watching (``docs/specs/runs.md``): the store is the record
and the only thing a watcher may believe. What is missing from that is
promptness -- a watcher that only read would have to read again and again, and
a person would see an answer arrive at the rate of the polling rather than at
the rate of the model.

So this port carries **no data at all**: it says "run *r* now has something at
position *s*", and the watcher goes and reads it. Nothing a signal says is
trusted, nothing is delivered through it, and a signal that is lost costs a
wait and never an event.

**One process today** (``robinauts.legacy.adapters.MemoryRunSignals``), one
PostgreSQL ``LISTEN``/``NOTIFY`` later (``docs/specs/runs.md``, "Details
likely to change"), and the application does not change between the two. That
is why:

- ``announce`` is a coroutine. A signal that travels through the database is
  sent by talking to it, and a port whose announcement could not await
  anything would have to be implemented by smuggling a queue behind it;
- ``announce`` carries a position and not an event, since a payload has a
  bound in PostgreSQL and a watcher reads the store regardless;
- **every wait is bounded, by the caller**. ``changed`` takes the bound it
  waits under and answers whether it was woken, so a signal that never
  arrives -- a listener that dropped its connection, an announcement lost
  between two processes -- degrades into a poll and can never become a
  watcher that hangs.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod


class RunSignals(ABC):
    """Saying that a run has moved, and waiting until one does."""

    @abstractmethod
    async def announce(self, run_id: uuid.UUID, seq: int, *, ended: bool = False) -> None:
        """Say that this run's stream now reaches ``seq``.

        Called by the application **after** the event at that position is
        stored, so that everything an announcement makes anybody look for is
        already there to be read. ``ended`` says that the position announced
        is the run's ``RunEnded``: there will be nothing after it, and an
        implementation may let go of whatever it was keeping for that run.

        It **never raises and never waits on anything slow**. Announcing is
        the last step of writing an event, and a signal that could fail would
        be a run that could not be written; the worst an implementation may do
        with a signal it cannot send is drop it, which costs the watchers one
        bounded wait each.
        """
        raise NotImplementedError

    @abstractmethod
    async def changed(self, run_id: uuid.UUID, after: int, *, timeout: float) -> bool:
        """Wait until that run has something past ``after``; whether it has.

        ``True`` when something past ``after`` was announced, or when the run
        was announced as ended -- both mean "go and read". ``False`` when
        ``timeout`` seconds passed with nothing said, which means "read
        anyway": a watcher re-reads after either answer, so neither is
        trusted and a lost signal is a slower stream rather than a broken one.

        It returns at once, without waiting, when what is already known says
        so -- a run announced past ``after`` before anybody asked, and a run
        already announced as ended.

        **Many watchers per run.** A run may be watched by every tab its
        author has open, and each of them waits here; an implementation wakes
        all of them and keeps nothing per watcher once it has gone.

        ``CancelledError`` passes through: a watcher whose reader went away is
        cancelled here, and it must stop rather than finish its wait.
        """
        raise NotImplementedError
