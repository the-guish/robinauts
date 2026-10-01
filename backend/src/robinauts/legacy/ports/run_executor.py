# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Where the work of a run happens: scheduling, and nothing about runs.

A run executes in the background and does not depend on the request that
started it (``docs/specs/runs.md``). This is the seam that makes that true:
the application hands over the work of a run and is answered at once, so a
request is served the moment the run exists and nothing outside the
application ever creates a task.

**It is about scheduling and not about meaning.** An executor is told a run's
id and given something to run; it knows nothing about turns, events, stores or
states, and it decides nothing about them. Which run may be executed, what
ending is written when work is cancelled, who may cancel -- all of that is the
application's, above this port. What is here is: run this, is that run's work
still going, stop it, stop everything.

**The id is a key and not a promise.** An executor holds at most one piece of
work per run id, which is what lets ``cancel`` and ``running`` be asked by id;
it is not where "one run is answered once" is decided -- the store is
(``robinauts.legacy.ports.ConversationStore``), and the application's own claim is
what covers the instant before work exists.

**Version 1 is asyncio tasks in the backend process**
(``robinauts.legacy.adapters.AsyncioRunExecutor``): there is no worker, no queue and
no scheduler (``docs/specs/backend.md``). A later ``robinauts worker``
claiming runs from the database would be another implementation of this, and
the application would not know.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Coroutine
from typing import Any

RunWork = Callable[[], Coroutine[Any, Any, None]]
"""What an executor is handed: a factory that makes the coroutine to run.

A factory rather than a coroutine, deliberately. A coroutine exists the moment
it is written down and must then be awaited by **somebody**: an executor that
refused one -- it is closing, it already has that run -- would leave a
coroutine nobody ever ran, which the interpreter complains about on its way
out and which a test run with ``-W error`` fails on. A factory that is never
called costs nothing, and the coroutine is made where it is run.
"""

RunReport = Callable[[float], Coroutine[Any, Any, None]]
"""What is run for work that never began, told when the close must be over.

The same kind of factory, and it is handed the **deadline** of the close that
is running it -- a moment on the running loop's clock
(``asyncio.get_running_loop().time()``), not a length of time -- because
``aclose(timeout=...)`` is how long the whole shutdown may take and not how
long each of these may. What a report owes the run it is about may be written
under a shield, where no cancellation can reach it; so the bound cannot be
imposed from outside, and it is **told** instead: a report comes back by the
deadline, leaving what it must to finish on its own and saying in the log that
it did. An executor waits for none of it past then, whatever the report does.
"""


class RunExecutor(ABC):
    """Work scheduled in the background, keyed by the run it belongs to."""

    @abstractmethod
    def submit(
        self, run_id: uuid.UUID, work: RunWork, *, never_began: RunReport | None = None
    ) -> None:
        """Start ``work`` for that run in the background; return at once.

        **Synchronous, and it waits for nothing.** Whoever calls it is
        answering a request: the run is already in the store, the answer to
        the request is the run, and what happens next happens without anybody
        holding on to it.

        The caller has already said that this run is its own
        (``application.Turns.claim``) -- there is an instant between "work was
        submitted" and "work has begun" in which an executor has the run and
        nothing has run, and a sweep looking in that instant must not decide
        that nobody is answering it.

        **That instant is also the one thing this must report.** Work given up
        on before it ever began -- an executor closing under it -- ran no line
        of the caller's code and wrote no ending anywhere, so ``never_began``
        is what is run instead: at the latest inside ``aclose``, before it
        returns, while the process still holds what that report needs. It is
        run once, and never for work that had a step: work that began is
        responsible for its own ending. It is given the close's deadline and is
        expected to respect it (``RunReport``), because a shutdown has one
        bound and these are inside it.

        ``RunAlreadyActiveError`` if this executor already has work for that
        run, ``InvalidValueError`` if it has been closed: a process that is
        stopping starts nothing new.
        """
        raise NotImplementedError

    @abstractmethod
    def cancel(self, run_id: uuid.UUID) -> bool:
        """Stop the work of that run if this executor has it; whether it did.

        ``False`` means only that **this** executor has no live work for that
        run -- it never ran here, it has finished, or it is somebody else's
        process. It is not "the run cannot be cancelled": what actually stops
        a run is its ended record in the store, which every process shares
        (``docs/specs/runs.md``), and the caller is what knows that.

        ``True`` means the work has been asked to stop, not that it has
        stopped. Work cancelled this way is expected to finish what it owes --
        the application's ending is written under a shield -- so the run is
        over a moment later, not at once. That expectation holds only of work
        that has **begun**: cancelling work that has not had a step runs none
        of it and writes no ending, which is why the application cancels only
        work it knows has begun and ends every other run in the store.
        """
        raise NotImplementedError

    @abstractmethod
    def running(self) -> frozenset[uuid.UUID]:
        """The runs whose work this executor is carrying, right now.

        For the start-up sweep, which must interrupt no run this process is
        answering, and for an operator asking what a process holds. Work that
        has finished is not in it.
        """
        raise NotImplementedError

    @abstractmethod
    async def aclose(self, *, timeout: float) -> None:
        """Stop everything and let go; the process is shutting down.

        Nothing is drained: this version cancels what is left and waits,
        bounded by ``timeout``, for the work to finish what it owes -- for a
        run that is the ending it writes under a shield, so a run whose
        process stops ends ``interrupted`` rather than staying ``running``
        (``docs/working-notes/poc-scope.md``: draining is not in this
        version). Work still going when the bound passes is abandoned, with a
        line in the log, and what it was answering is left to the sweep of the
        next start-up.

        **``timeout`` bounds the whole close**, not each piece of it: the wait
        for the cancelled work and the reports of the work that never began
        share one deadline, because a bound multiplied by however many runs a
        process was carrying is not a bound. Whoever asks for one asks how
        long the process may take to stop.

        It never raises over what the work did: at shutdown there is nobody to
        report to, and what a task raised on its way out is a log line.
        ``InvalidValueError`` if the bound is not a number of seconds, which is
        a mistake in the caller and not something the shutdown did. It is
        idempotent, and an executor that has been closed starts nothing new.
        """
        raise NotImplementedError
